"""Concurrent ``migrate()`` must not race or corrupt ``alembic_version``.

``migrate()`` runs on startup from both the FastAPI process and the worker, and
from every web instance.  A Postgres session-scoped advisory lock acquired on a
dedicated connection serializes the body so only one process applies at a time;
the other blocks, then Alembic reads ``alembic_version`` and finds it is already
at head.
"""

from __future__ import annotations

import asyncio

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

import app.core.database as database


@pytest.mark.asyncio
async def test_concurrent_migrate_does_not_duplicate(_migrated_schema: None) -> None:
    """Parallel migrate() calls are idempotent — no duplicate application.

    The schema is already at head (via _migrated_schema).  Running two
    concurrent migrate() calls must complete safely: the advisory lock serializes
    them, and each call either applies (first one wins) or no-ops (second sees
    alembic_version already at head).
    """
    # Both calls should complete without error.
    await asyncio.gather(database.migrate(), database.migrate())

    async with database.get_engine().connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        rows = result.all()

    # Exactly one row, containing the current Alembic head. Reading the head
    # from the script directory keeps the assertion resilient to new revisions.
    expected_head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert len(rows) == 1, f"expected exactly 1 alembic_version row, got {rows}"
    assert rows[0][0] == expected_head, f"unexpected revision: {rows[0][0]}"
