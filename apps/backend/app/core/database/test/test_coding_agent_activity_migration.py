"""Post-migrate() invariants for the partitioned ``coding_agent_activity`` table.

Asserts the state that ``migrate()`` delivers: the partitioned parent exists,
``maintain_coding_agent_activity_partitions()`` has seeded the current ISO-week
child partition and the next two, and rows with ``created_at = now()`` route to
the correct child.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core import database
from app.core.database.service import (
    _coding_agent_activity_partition_ddl,
    _coding_agent_activity_week_start,
)


def test_partition_ddl_names_are_deterministic_for_a_known_utc_date() -> None:
    """The shared helper derives partition names + bounds from one UTC anchor.

    2026-06-08 (Monday) is ISO year 2026, week 24; the ``(0, +1, +2)`` window
    yields weeks 24/25/26, each ``coding_agent_activity_pYYYYWW``."""
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
    """The parent table is partitioned by RANGE on created_at after migrate()."""
    engine = database.get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT relkind FROM pg_class WHERE relname = 'coding_agent_activity'")
        )
        row = result.one_or_none()
    assert row is not None, "coding_agent_activity should exist"
    # relkind 'p' = partitioned table
    relkind = row[0]
    if isinstance(relkind, bytes):
        relkind = relkind.decode("ascii")
    assert relkind == "p", "expected coding_agent_activity to be a partitioned table"


@pytest.mark.asyncio
async def test_initial_partitions_seeded_by_migrate(_migrated_schema: None) -> None:
    """migrate() calls maintain_coding_agent_activity_partitions() which seeds >= 3 child partitions."""
    engine = database.get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE p.relname = 'coding_agent_activity'"
            )
        )
        names = [row[0] for row in result]
    assert len(names) >= 3, f"expected >= 3 weekly child partitions after migrate(), got {names}"


@pytest.mark.asyncio
async def test_row_at_now_lands_in_current_week_child(_migrated_schema: None) -> None:
    """A row with ``created_at = now()`` inserts into the current-week child partition."""
    week_start = _coding_agent_activity_week_start(datetime.now(UTC))
    iso_year, iso_week, _ = week_start.isocalendar()
    current_child = f"coding_agent_activity_p{iso_year:04d}{iso_week:02d}"

    engine = database.get_engine()
    run_id = uuid.uuid4()
    org_id = uuid.uuid4()
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
