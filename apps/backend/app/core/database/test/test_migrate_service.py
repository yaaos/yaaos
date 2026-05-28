"""Concurrent `migrate()` must not double-apply a version.

`migrate()` runs on startup from both the FastAPI process and the worker, and
from every web instance. Without serialization the callers can both read an
empty `applied` set, race to
apply the same DDL, and one of them crashes on the duplicate
`INSERT INTO schema_migrations` PK. A Postgres session-scoped advisory lock
acquired on a dedicated connection serializes the body so only one process
applies at a time; the other blocks, then re-reads and no-ops.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.core import database
from app.core.database.service import _MIGRATIONS


@pytest.mark.asyncio
async def test_concurrent_migrate_does_not_duplicate(_migrated_schema: None) -> None:
    engine = database.get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE schema_migrations"))

    await asyncio.gather(database.migrate(), database.migrate())

    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT version, COUNT(*) AS n FROM schema_migrations GROUP BY version")
        )
        counts = {row[0]: row[1] for row in result}

    expected = {v for v, _ in _MIGRATIONS}
    assert set(counts) == expected
    duplicates = {v: n for v, n in counts.items() if n != 1}
    assert not duplicates, f"versions applied more than once: {duplicates}"
