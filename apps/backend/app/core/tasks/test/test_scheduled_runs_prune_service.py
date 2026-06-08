"""Service-tier guard for the daily `scheduled_runs` prune task.

Two invariants:
  - The prune body is registered with the taskiq broker under the public
    task name (the `@scheduled` decorator wires the `@task` step).
  - The body deletes `scheduled_runs` rows older than 7 days, leaves
    fresher rows alone — verified by inserting rows with controlled
    `created_at` values via raw SQL (the timestamp default is
    server-side `now()`, so backdating requires an explicit value).

Both tests bypass the standard transactional-rollback fixture because
the prune body opens its own session and commits; they hand-clean
their inserted rows pre/post to avoid cross-test leakage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.core.database import get_sessionmaker
from app.core.tasks.broker import get_broker
from app.core.tasks.models import ScheduledRunRow
from app.core.tasks.scheduled_runs_prune import prune_scheduled_runs

_SEED_IDS = ["test_prune_old_row", "test_prune_recent_row"]


async def _clean_seed_rows() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await s.execute(delete(ScheduledRunRow).where(ScheduledRunRow.schedule_id.in_(_SEED_IDS)))
        await s.commit()


@pytest_asyncio.fixture
async def _clean_prune_seed() -> AsyncIterator[None]:
    await _clean_seed_rows()
    yield
    await _clean_seed_rows()


@pytest.mark.service
@pytest.mark.asyncio
async def test_prune_task_registered_with_broker() -> None:
    """The prune body is registered with the taskiq broker under its
    public task name so the scheduler tick + outbox drain can find +
    dispatch it. The `@scheduled` decorator wires `@task`; this guard
    catches a regression where that step stops happening."""
    assert get_broker().find_task("scheduled_runs_prune") is not None


@pytest.mark.service
@pytest.mark.asyncio
async def test_prune_deletes_rows_older_than_seven_days(_clean_prune_seed: None) -> None:
    """Rows with `created_at` >7 days ago are deleted; fresher rows
    survive."""
    sessionmaker = get_sessionmaker()
    now = datetime.now(UTC)
    old_slot = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
    recent_slot = datetime(2027, 6, 7, 12, 0, 0, tzinfo=UTC)
    schedule_id_old, schedule_id_recent = _SEED_IDS

    # Seed: one >7-day row (backdated created_at via raw SQL) and one
    # fresh row.
    async with sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO scheduled_runs (schedule_id, fire_time, created_at) "
                "VALUES (:sid, :fire, :created)"
            ),
            {"sid": schedule_id_old, "fire": old_slot, "created": now - timedelta(days=30)},
        )
        await s.execute(
            text(
                "INSERT INTO scheduled_runs (schedule_id, fire_time, created_at) "
                "VALUES (:sid, :fire, :created)"
            ),
            {"sid": schedule_id_recent, "fire": recent_slot, "created": now - timedelta(days=1)},
        )
        await s.commit()

    # Invoke the prune body directly. It opens its own session + commits.
    await prune_scheduled_runs()

    async with sessionmaker() as s:
        survivors = (
            (await s.execute(select(ScheduledRunRow).where(ScheduledRunRow.schedule_id.in_(_SEED_IDS))))
            .scalars()
            .all()
        )
        survivor_ids = {r.schedule_id for r in survivors}
        assert schedule_id_old not in survivor_ids, "old row should have been pruned"
        assert schedule_id_recent in survivor_ids, "recent row should have survived"
