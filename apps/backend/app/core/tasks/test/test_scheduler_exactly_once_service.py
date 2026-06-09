"""Service-tier guard for the recurring-task scheduler's exactly-once claim.

The scheduler's headline invariant: for one normalized fire slot
`(schedule_id, fire_time)`, exactly one worker's `INSERT … ON CONFLICT
DO NOTHING` row wins, and only that worker enqueues. This test races N
concurrent `tick_once` calls on N independent sessions against the real
Postgres and asserts:

  - exactly one row appears in `scheduled_runs` for the slot, and
  - exactly one row appears in `outbox_entries` (a successful enqueue).

The N independent-sessions setup is essential: the standard `db_session`
fixture wraps everything in a single connection + outer transaction, so
concurrent inserts on it would serialize through savepoints and never
race the `ON CONFLICT`. We open N sessions off the live engine, race
them, and clean up both before and after to be robust against residue
from a previous (interrupted) run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.database import get_sessionmaker
from app.core.tasks import task, tick_once
from app.core.tasks.models import OutboxEntryRow, ScheduledRunRow
from app.core.tasks.scheduler import schedule_task
from app.core.tasks.service import scoped_task_registration


async def _clean(schedule_ids: list[str], task_names: list[str]) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await s.execute(delete(ScheduledRunRow).where(ScheduledRunRow.schedule_id.in_(schedule_ids)))
        # Outbox cleanup matches by task_name inside payload to avoid
        # touching rows from other tests' tasks. JSONB '->>' extracts as
        # text.
        for name in task_names:
            await s.execute(
                delete(OutboxEntryRow).where(
                    OutboxEntryRow.kind == "taskiq_enqueue",
                    OutboxEntryRow.payload["task_name"].astext == name,
                )
            )
        await s.commit()


@pytest_asyncio.fixture
async def _clean_exactly_once() -> AsyncIterator[None]:
    ids = ["test_exactly_once_schedule"]
    names = ["test_exactly_once_task"]
    await _clean(ids, names)
    yield
    await _clean(ids, names)


@pytest_asyncio.fixture
async def _clean_skip() -> AsyncIterator[None]:
    ids = ["test_skip_when_no_match"]
    names = ["test_skip_task"]
    await _clean(ids, names)
    yield
    await _clean(ids, names)


@pytest_asyncio.fixture
async def _clean_second_tick() -> AsyncIterator[None]:
    ids = ["test_second_tick_no_op"]
    names = ["test_second_tick_task"]
    await _clean(ids, names)
    yield
    await _clean(ids, names)


@pytest.mark.service
@pytest.mark.asyncio
async def test_scheduler_exactly_once_under_concurrent_ticks(_clean_exactly_once: None) -> None:
    """N concurrent `tick_once` calls for one fire slot → exactly one
    enqueue. The `INSERT … ON CONFLICT DO NOTHING` claim is the sole
    gate; verified by counting both `scheduled_runs` and `outbox_entries`
    rows after the race."""

    async def _body(*, slot: str) -> None:
        del slot

    schedule_id = "test_exactly_once_schedule"
    # The cron `* * * * *` matches every minute, so pinning `now` to any
    # value gives a deterministic slot.
    slot = datetime(2027, 1, 1, 12, 0, 0, tzinfo=UTC)

    ref = task("test_exactly_once_task")(_body)
    sessionmaker = get_sessionmaker()

    with scoped_task_registration(ref):
        schedule_task(schedule_id, "* * * * *", task_ref=ref)
        n = 8

        async def _one_tick() -> list[str]:
            async with sessionmaker() as s:
                fired = await tick_once(session=s, now=slot)
                await s.commit()
                return fired

        results = await asyncio.gather(*[_one_tick() for _ in range(n)])

        winners = [f for r in results for f in r]
        assert len(winners) == 1, (
            f"expected exactly one tick to claim+enqueue the slot, got {len(winners)} "
            f"(per-tick results: {results})"
        )
        assert winners[0] == schedule_id

        async with sessionmaker() as s:
            claim_rows = (
                (await s.execute(select(ScheduledRunRow).where(ScheduledRunRow.schedule_id == schedule_id)))
                .scalars()
                .all()
            )
            assert len(claim_rows) == 1
            assert claim_rows[0].fire_time.replace(tzinfo=UTC) == slot

            outbox_rows = (
                (
                    await s.execute(
                        select(OutboxEntryRow).where(
                            OutboxEntryRow.kind == "taskiq_enqueue",
                            OutboxEntryRow.payload["task_name"].astext == "test_exactly_once_task",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(outbox_rows) == 1, f"expected exactly one outbox enqueue, got {len(outbox_rows)}"


@pytest.mark.service
@pytest.mark.asyncio
async def test_scheduler_skips_when_cron_does_not_match(_clean_skip: None) -> None:
    """A non-matching cron must not enqueue. Verified by passing a
    `now` that the cron explicitly excludes."""
    schedule_id = "test_skip_when_no_match"

    async def _body() -> None: ...

    ref = task("test_skip_task")(_body)
    sessionmaker = get_sessionmaker()

    with scoped_task_registration(ref):
        # Fires only at minute 30; pass minute 15.
        schedule_task(schedule_id, "30 * * * *", task_ref=ref)
        slot = datetime(2027, 1, 1, 12, 15, 0, tzinfo=UTC)

        async with sessionmaker() as s:
            fired = await tick_once(session=s, now=slot)
            await s.commit()
            assert fired == []

            claim_rows = (
                (await s.execute(select(ScheduledRunRow).where(ScheduledRunRow.schedule_id == schedule_id)))
                .scalars()
                .all()
            )
            assert claim_rows == []


@pytest.mark.service
@pytest.mark.asyncio
async def test_scheduler_second_tick_same_slot_is_no_op(_clean_second_tick: None) -> None:
    """A second tick at the same slot must NOT re-enqueue (the claim
    row is the gate). Sequential, single-session — proves the gate
    short-circuits the loser branch independent of concurrency."""
    schedule_id = "test_second_tick_no_op"

    async def _body() -> None: ...

    ref = task("test_second_tick_task")(_body)
    sessionmaker = get_sessionmaker()

    with scoped_task_registration(ref):
        schedule_task(schedule_id, "* * * * *", task_ref=ref)
        slot = datetime(2027, 1, 1, 12, 0, 0, tzinfo=UTC)

        async with sessionmaker() as s:
            fired_1 = await tick_once(session=s, now=slot)
            await s.commit()
            fired_2 = await tick_once(session=s, now=slot)
            await s.commit()
            assert fired_1 == [schedule_id]
            assert fired_2 == []

            outbox_rows = (
                (
                    await s.execute(
                        select(OutboxEntryRow).where(
                            OutboxEntryRow.kind == "taskiq_enqueue",
                            OutboxEntryRow.payload["task_name"].astext == "test_second_tick_task",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(outbox_rows) == 1
