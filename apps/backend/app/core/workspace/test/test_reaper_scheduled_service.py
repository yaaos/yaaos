"""Service-tier guard for the per-minute `workspace_reaper` `@scheduled` task.

Two invariants:

  - The reaper body is registered with the taskiq broker under the
    public task name (the `@scheduled` decorator wires the `@task` step).
  - `tick_once` at every-minute slot wins the per-tick claim and
    enqueues exactly one outbox row; a second tick at the same slot does
    NOT re-enqueue.

Tests open their own sessions off the live engine — the standard
`db_session` rollback fixture serializes through savepoints, defeating
the `ON CONFLICT` race the scheduler relies on. Raw SQL is used for
cleanup of `core/tasks`-owned tables (`scheduled_runs`, `outbox_entries`)
because cross-module Row imports are tach-forbidden; test files are
allowlisted out of `bin/check_table_access` raw-SQL ownership scan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import get_sessionmaker
from app.core.tasks import get_broker, get_pending_task_names, schedule_task, tick_once
from app.core.workspace.service import run_workspace_reaper, workspace_reaper

_SCHEDULE_ID = "workspace_reaper"
_TASK_NAME = "workspace_reaper"


async def _clean() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await s.execute(
            text("DELETE FROM scheduled_runs WHERE schedule_id = :sid"),
            {"sid": _SCHEDULE_ID},
        )
        await s.execute(
            text(
                "DELETE FROM outbox_entries WHERE kind = 'taskiq_enqueue' "
                "AND (payload ->> 'task_name') = :name"
            ),
            {"name": _TASK_NAME},
        )
        await s.commit()


@pytest_asyncio.fixture
async def _clean_seed() -> AsyncIterator[None]:
    await _clean()
    yield
    await _clean()


@pytest.mark.service
@pytest.mark.asyncio
async def test_workspace_reaper_task_registered_with_broker() -> None:
    """The reaper body is registered with the broker under its public
    task name. Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None


@pytest.mark.service
@pytest.mark.asyncio
async def test_workspace_reaper_fires_every_minute(_clean_seed: None) -> None:
    """The cron `* * * * *` matches every minute. A `tick_once` at any
    floored-minute slot wins the claim and enqueues; a second tick at
    the same slot is a no-op. Two distinct minute slots each enqueue
    once."""
    schedule_task(_SCHEDULE_ID, "* * * * *", task_ref=workspace_reaper)

    sessionmaker = get_sessionmaker()
    slot_a = datetime(2027, 1, 1, 12, 0, 0, tzinfo=UTC)
    slot_b = datetime(2027, 1, 1, 12, 1, 0, tzinfo=UTC)

    async with sessionmaker() as s:
        fired_a1 = await tick_once(session=s, now=slot_a)
        await s.commit()
        fired_a2 = await tick_once(session=s, now=slot_a)
        await s.commit()
        fired_b = await tick_once(session=s, now=slot_b)
        await s.commit()

        assert fired_a1 == [_SCHEDULE_ID]
        assert fired_a2 == []
        assert fired_b == [_SCHEDULE_ID]

        names = await get_pending_task_names(s)
        assert names.count(_TASK_NAME) == 2, f"expected two enqueues, got {names}"


@pytest.mark.service
@pytest.mark.asyncio
async def test_workspace_reaper_body_runs_idempotently() -> None:
    """The body wraps `_reaper_sweep_once`, which reads fresh state from
    the DB on every pass — empty DB stays empty. Surfaces an exception
    loudly: if a sweep step regresses the body raises and CI fails."""
    await run_workspace_reaper()
    await run_workspace_reaper()
