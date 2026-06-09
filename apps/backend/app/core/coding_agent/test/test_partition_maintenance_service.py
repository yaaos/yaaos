"""Service-tier guard for the daily `coding_agent_activity_partition_maintenance`
`@scheduled` task.

The migration seeds the same window as the maintenance task — the current
ISO-UTC week through +2 — so a fresh DB and a long-running one have
identical create-ahead. The rolling-window task keeps that coverage
continuous and drops partitions whose week is more than 4 weeks before
the current week.

Invariants:

  - The body is registered with the taskiq broker under the public task
    name (the `@scheduled` decorator wires the `@task` step).
  - `tick_once` at the daily 01:00 UTC slot wins the per-tick claim once.
  - The maintenance body is idempotent under double-fire — running twice
    leaves the same partition set as running once.
  - The body keeps the current week + the next two covered (the shared
    `(0, +1, +2)` window).
  - The body drops a partition whose ISO week is older than 4 weeks
    before the current week.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.coding_agent.partition_maintenance import (
    coding_agent_activity_partition_maintenance,
)
from app.core.database import get_engine, get_sessionmaker, maintain_coding_agent_activity_partitions
from app.core.tasks import get_broker, get_pending_task_names, schedule_task, tick_once

_SCHEDULE_ID = "coding_agent_activity_partition_maintenance"
_TASK_NAME = "coding_agent_activity_partition_maintenance"


async def _clean_dedup_and_outbox() -> None:
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
    await _clean_dedup_and_outbox()
    yield
    await _clean_dedup_and_outbox()


async def _list_partitions() -> list[str]:
    engine = get_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE p.relname = 'coding_agent_activity'"
                )
            )
        ).all()
    return sorted(row[0] for row in rows)


def _iso_key_for(when: datetime) -> int:
    iso_year, iso_week, _ = when.isocalendar()
    return iso_year * 100 + iso_week


@pytest.mark.service
@pytest.mark.asyncio
async def test_partition_maintenance_task_registered_with_broker(_migrated_schema: None) -> None:
    """The body is registered with the broker under its public task name.
    Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None


@pytest.mark.service
@pytest.mark.asyncio
async def test_partition_maintenance_fires_at_daily_slot(_migrated_schema: None, _clean_seed: None) -> None:
    """The cron `0 1 * * *` matches at 01:00 UTC. A `tick_once` pinned to
    that slot wins the claim and enqueues exactly one outbox row; a
    second tick at the same slot is a no-op."""
    schedule_task(_SCHEDULE_ID, "0 1 * * *", task_ref=coding_agent_activity_partition_maintenance)

    sessionmaker = get_sessionmaker()
    slot = datetime(2027, 1, 1, 1, 0, 0, tzinfo=UTC)

    async with sessionmaker() as s:
        fired_1 = await tick_once(session=s, now=slot)
        await s.commit()
        fired_2 = await tick_once(session=s, now=slot)
        await s.commit()
        assert fired_1 == [_SCHEDULE_ID]
        assert fired_2 == []

        names = await get_pending_task_names(s)
        assert names.count(_TASK_NAME) == 1, f"expected one enqueue, got {names}"


@pytest.mark.service
@pytest.mark.asyncio
async def test_maintain_creates_two_weeks_ahead(_migrated_schema: None) -> None:
    """The maintenance body's create-ahead window covers current week +
    next two — the shared `(0, +1, +2)` window. The current-week-plus-two
    partition exists after the body runs."""
    now = datetime.now(UTC)
    today_midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    week_start = today_midnight - timedelta(days=today_midnight.weekday())
    plus_two_iso_key = _iso_key_for(week_start + timedelta(weeks=2))
    plus_two_name = f"coding_agent_activity_p{plus_two_iso_key:06d}"

    # The migration seed never includes +2, but `maintain` commits it to the
    # shared DB and partition DDL is not transaction-rolled-back — so a prior
    # run this ISO week may have left it. Drop it for a deterministic snapshot
    # (mirrors how the drop test self-manages its synthetic partition).
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {plus_two_name}"))

    before = await _list_partitions()
    assert plus_two_name not in before, f"+2 partition should be absent after the pre-drop; before={before}"

    await maintain_coding_agent_activity_partitions()

    after = await _list_partitions()
    assert plus_two_name in after, f"expected {plus_two_name} after maintenance; after={after}"


@pytest.mark.service
@pytest.mark.asyncio
async def test_maintain_drops_partitions_older_than_four_weeks(_migrated_schema: None) -> None:
    """A partition whose ISO week is more than 4 weeks before the current
    week is dropped by the maintenance pass; a partition 1 week old (the
    migration's prev-week seed) survives."""
    now = datetime.now(UTC)
    today_midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    week_start = today_midnight - timedelta(days=today_midnight.weekday())

    # Seed a synthetic partition for a week 6 weeks in the past.
    old_lower = week_start - timedelta(weeks=6)
    old_upper = old_lower + timedelta(weeks=1)
    old_iso_year, old_iso_week, _ = old_lower.isocalendar()
    old_name = f"coding_agent_activity_p{old_iso_year:04d}{old_iso_week:02d}"
    lower_lit = old_lower.strftime("%Y-%m-%d %H:%M:%S+00")
    upper_lit = old_upper.strftime("%Y-%m-%d %H:%M:%S+00")

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {old_name} "
                f"PARTITION OF coding_agent_activity "
                f"FOR VALUES FROM ('{lower_lit}') TO ('{upper_lit}')"
            )
        )

    before = await _list_partitions()
    assert old_name in before

    await maintain_coding_agent_activity_partitions()

    after = await _list_partitions()
    assert old_name not in after, f"expected {old_name} to be dropped; after={after}"

    # The current-week partition (offset 0, in both the seed and maintenance
    # windows) survives — only weeks strictly older than 4 weeks back are dropped.
    current_iso_key = _iso_key_for(week_start)
    current_name = f"coding_agent_activity_p{current_iso_key:06d}"
    assert current_name in after, (
        f"current-week partition {current_name} should survive (only >4-weeks dropped); after={after}"
    )


@pytest.mark.service
@pytest.mark.asyncio
async def test_maintain_idempotent_under_double_fire(_migrated_schema: None) -> None:
    """Running the body twice leaves the same partition set as running
    it once. Critical because the broker may redeliver the body after a
    drain crash between Redis push and `dispatched_at` stamp."""
    await maintain_coding_agent_activity_partitions()
    after_first = await _list_partitions()
    await maintain_coding_agent_activity_partitions()
    after_second = await _list_partitions()
    assert after_first == after_second
