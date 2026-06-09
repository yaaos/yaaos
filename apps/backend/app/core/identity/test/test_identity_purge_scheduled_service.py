"""Service-tier guard for the hourly `identity_purge` `@scheduled` task.

Two invariants:

  - The purge body is registered with the taskiq broker under the public
    task name (the `@scheduled` decorator wires the `@task` step).
  - `tick_once` at the top-of-hour slot wins the per-tick claim once and
    enqueues exactly one outbox row; a second tick at the same slot does
    NOT re-enqueue (the claim row is the gate).

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
from app.core.identity.scheduler import identity_purge, run_identity_purge
from app.core.tasks import get_broker, get_pending_task_names, schedule_task, tick_once

_SCHEDULE_ID = "identity_purge"
_TASK_NAME = "identity_purge"


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
async def test_identity_purge_task_registered_with_broker() -> None:
    """The purge body is registered with the broker under its public
    task name so the scheduler tick + outbox drain can find + dispatch
    it. Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None


@pytest.mark.service
@pytest.mark.asyncio
async def test_identity_purge_fires_on_top_of_hour(_clean_seed: None) -> None:
    """The cron `0 * * * *` matches at minute 0 of any hour. A `tick_once`
    pinned to that slot wins the claim and enqueues exactly one outbox
    row. A second tick at the same slot is a no-op (claim already taken)."""
    # Re-register the schedule explicitly — the autouse
    # `scheduler_registry_isolation` fixture wipes the registry per test.
    schedule_task(_SCHEDULE_ID, "0 * * * *", task_ref=identity_purge)

    sessionmaker = get_sessionmaker()
    slot = datetime(2027, 1, 1, 12, 0, 0, tzinfo=UTC)

    async with sessionmaker() as s:
        fired_1 = await tick_once(session=s, now=slot)
        await s.commit()
        fired_2 = await tick_once(session=s, now=slot)
        await s.commit()
        assert fired_1 == [_SCHEDULE_ID]
        assert fired_2 == []

        names = await get_pending_task_names(s)
        assert names.count(_TASK_NAME) == 1, f"expected exactly one enqueue, got {names}"


@pytest.mark.service
@pytest.mark.asyncio
async def test_identity_purge_skips_non_top_of_hour(_clean_seed: None) -> None:
    """The cron `0 * * * *` does NOT match minute 30. A tick pinned to
    minute 30 fires nothing."""
    schedule_task(_SCHEDULE_ID, "0 * * * *", task_ref=identity_purge)
    sessionmaker = get_sessionmaker()
    slot = datetime(2027, 1, 1, 12, 30, 0, tzinfo=UTC)

    async with sessionmaker() as s:
        fired = await tick_once(session=s, now=slot)
        await s.commit()
        assert fired == []


@pytest.mark.service
@pytest.mark.asyncio
async def test_identity_purge_body_runs_idempotently() -> None:
    """The body itself is idempotent — bare `DELETE … WHERE created_at <
    cutoff` is a no-op on an empty DB and stays a no-op on re-invocation.
    Surfaces an exception loudly: if a purge step regresses the body
    raises and CI fails."""
    await run_identity_purge()
    await run_identity_purge()
