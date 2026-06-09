"""Partitioned-table migration for `coding_agent_activity`.

The codebase's first partitioned table — DDL is `PARTITION BY RANGE
(created_at)` + ~2 weeks of `PARTITION OF` children. The migration must
be idempotent under double-fire so re-running the migrator after a
partial application is safe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core import database
from app.core.database.service import (
    _apply_create_coding_agent_activity,
    _coding_agent_activity_partition_ddl,
    _coding_agent_activity_week_start,
)


def test_partition_ddl_names_are_deterministic_for_a_known_utc_date() -> None:
    """The shared helper derives partition names + bounds from one UTC anchor.

    2026-06-08 (Monday) is ISO year 2026, week 24; the `(0, +1, +2)` window
    yields weeks 24/25/26, each `coding_agent_activity_pYYYYWW`."""
    # A Wednesday — week-start floors back to Monday 2026-06-08.
    now = datetime(2026, 6, 10, 15, 30, tzinfo=UTC)
    assert _coding_agent_activity_week_start(now) == datetime(2026, 6, 8, tzinfo=UTC)

    ddl = _coding_agent_activity_partition_ddl(now)
    assert len(ddl) == 3
    assert "coding_agent_activity_p202624" in ddl[0]
    assert "FROM ('2026-06-08 00:00:00+00') TO ('2026-06-15 00:00:00+00')" in ddl[0]
    assert "coding_agent_activity_p202625" in ddl[1]
    assert "coding_agent_activity_p202626" in ddl[2]
    # No backdated (-1) week: the seed window starts at offset 0.
    assert all("p202623" not in stmt for stmt in ddl)


@pytest.mark.asyncio
async def test_coding_agent_activity_is_partitioned(_migrated_schema: None) -> None:
    """The parent table is partitioned by RANGE on created_at."""
    engine = database.get_engine()
    async with engine.connect() as conn:
        # `partstrat` = 'r' (range) when partitioned by range; absent otherwise.
        result = await conn.execute(
            text(
                "SELECT pt.partstrat FROM pg_partitioned_table pt"
                " JOIN pg_class c ON c.oid = pt.partrelid"
                " WHERE c.relname = 'coding_agent_activity'"
            )
        )
        row = result.one_or_none()
    assert row is not None, "coding_agent_activity should be partitioned"
    # `partstrat` is Postgres `char` type — driver returns bytes (`b'r'`).
    strat = row[0]
    if isinstance(strat, bytes):
        strat = strat.decode("ascii")
    assert strat == "r", "expected RANGE partitioning"


@pytest.mark.asyncio
async def test_initial_partitions_exist(_migrated_schema: None) -> None:
    """The migration creates at least three weekly child partitions."""
    engine = database.get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT relname FROM pg_class WHERE relname LIKE 'coding_agent_activity_p%' AND relkind = 'r'"
            )
        )
        names = [row[0] for row in result]
    assert len(names) >= 3, f"expected >= 3 weekly partitions, got {names}"


@pytest.mark.asyncio
async def test_migration_idempotent_under_double_fire(_migrated_schema: None) -> None:
    """Re-running `_apply_create_coding_agent_activity` after the initial
    migration must succeed (CREATE TABLE IF NOT EXISTS at every level)."""
    engine = database.get_engine()
    async with engine.begin() as conn:
        # Second fire on top of the already-applied migration; raises on
        # duplicate-table errors if any CREATE lacked IF NOT EXISTS.
        await _apply_create_coding_agent_activity(conn)

    # Verify the table + partitions still exist after the double-fire.
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_class"
                " WHERE relname = 'coding_agent_activity'"
                " AND relkind = 'p'"  # 'p' = partitioned table
            )
        )
        count = result.scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_row_at_now_lands_in_current_week_child(_migrated_schema: None) -> None:
    """A row with `created_at = now()` inserts into the partitioned parent —
    i.e. the current-week child partition exists and covers it."""
    week_start = _coding_agent_activity_week_start(datetime.now(UTC))
    iso_year, iso_week, _ = week_start.isocalendar()
    current_child = f"coding_agent_activity_p{iso_year:04d}{iso_week:02d}"

    engine = database.get_engine()
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
    # Run inside a transaction rolled back at the end — partition DDL is already
    # committed by the migration; only these data rows need cleanup.
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO coding_agent_runs "
                    "(id, org_id, workflow_execution_id, step_id, agent_command_id, "
                    " command_kind, plugin_id, status) "
                    "VALUES (:id, :org, :wfe, 'review', :cmd, 'review', 'claude_code', 'running')"
                ),
                {"id": run_id, "org": org_id, "wfe": uuid.uuid4(), "cmd": uuid.uuid4()},
            )
            await conn.execute(
                text(
                    "INSERT INTO coding_agent_activity (run_id, created_at, org_id, payload) "
                    "VALUES (:rid, now(), :org, '{}'::jsonb)"
                ),
                {"rid": run_id, "org": org_id},
            )
            # The row must be physically routed to the current-week child.
            landed = (
                await conn.execute(
                    text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                        f"SELECT count(*) FROM {current_child} WHERE run_id = :rid"
                    ),
                    {"rid": run_id},
                )
            ).scalar_one()
            assert landed == 1, f"row should land in {current_child}"
        finally:
            await trans.rollback()
