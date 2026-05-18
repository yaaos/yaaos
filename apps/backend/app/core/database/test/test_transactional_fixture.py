"""Smoke tests for the `db_session` transactional-rollback fixture.

Confirms two invariants:

1. Writes done by production-style `async with session() as s` calls during a
   test land in the same transaction as the fixture-bound session — visible
   inside the test, gone after teardown.
2. Two sequential tests using the fixture don't see each other's writes
   (i.e. rollback actually rolls back).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.database import session


@pytest.mark.asyncio
async def test_writes_inside_fixture_are_visible_via_session_helper(db_session) -> None:  # type: ignore[no-untyped-def]
    """Production code's `async with session() as s:` honors the fixture's session."""
    marker = f"yaaos_test_{uuid.uuid4()}"
    # Use a scratch table that's guaranteed to exist (schema_migrations).
    async with session() as s:
        await s.execute(
            text("INSERT INTO schema_migrations(version) VALUES (:v)"),
            {"v": marker},
        )
        await s.commit()

    # The fixture session sees it too (same transaction).
    found = (
        await db_session.execute(
            text("SELECT version FROM schema_migrations WHERE version = :v"),
            {"v": marker},
        )
    ).first()
    assert found is not None
    assert found[0] == marker


@pytest.mark.asyncio
async def test_rollback_isolates_subsequent_test(db_session) -> None:  # type: ignore[no-untyped-def]
    """If the previous test's write rolled back, we won't see it here."""
    # Look for any row that matches the marker pattern from the previous test;
    # none should remain after rollback.
    rows = (
        await db_session.execute(
            text("SELECT version FROM schema_migrations WHERE version LIKE 'yaaos_test_%'")
        )
    ).all()
    assert rows == []
