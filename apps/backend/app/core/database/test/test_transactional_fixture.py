"""Smoke tests for the ``db_session`` transactional-rollback fixture.

Covers three invariants:

- Writes done via the production ``session()`` helper are visible to the
  fixture session within the same test (same outer transaction).
- A test's writes survive only until the fixture rolls back at teardown —
  they are not committed to the real DB.
- The next sequential test does not see the prior test's writes (true
  cross-test isolation).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.core.database import session

# Stable sentinel written by test_writes_a_sentinel_for_isolation_check and
# queried (for absence) by test_rollback_isolates_subsequent_test.
_ISOLATION_SENTINEL = "yaaos_iso_test_marker"


@pytest.mark.asyncio
async def test_writes_inside_fixture_are_visible_via_session_helper(db_session) -> None:  # type: ignore[no-untyped-def]
    """Production code's ``async with session() as s:`` honors the fixture's session."""
    # Create a temp scratch table inside the transaction so we don't depend on
    # any particular app table's constraints (alembic_version has a 32-char limit;
    # schema_migrations no longer exists).  The temp table exists only for the
    # duration of this transaction and is discarded with it on rollback.
    async with session() as s:
        await s.execute(
            text("CREATE TEMP TABLE IF NOT EXISTS _test_scratch (v TEXT NOT NULL) ON COMMIT DROP")
        )
        marker = f"yaaos_test_{uuid.uuid4()}"
        await s.execute(
            text("INSERT INTO _test_scratch(v) VALUES (:v)"),
            {"v": marker},
        )
        await s.commit()

    # The fixture session sees it too (same transaction).
    found = (
        await db_session.execute(
            text("SELECT v FROM _test_scratch WHERE v = :v"),
            {"v": marker},
        )
    ).first()
    assert found is not None
    assert found[0] == marker


@pytest.mark.asyncio
async def test_writes_a_sentinel_for_isolation_check(db_session) -> None:  # type: ignore[no-untyped-def]
    """Insert a sentinel lessons row via the production session() helper.

    No explicit commit — the fixture's outer transaction is what holds this
    write.  At teardown the fixture rolls back, making the row disappear.
    The following test queries for it and asserts absence, which proves
    the rollback actually happened.
    """
    async with session() as s:
        await s.execute(
            text("INSERT INTO lessons (org_id, title, body) VALUES (:org_id, :title, :body)"),
            {
                "org_id": str(uuid.uuid4()),
                "title": _ISOLATION_SENTINEL,
                "body": "isolation test body",
            },
        )
        await s.commit()

    # Confirm the row is visible within this test's transaction.
    count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM lessons WHERE title = :t"),
            {"t": _ISOLATION_SENTINEL},
        )
    ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_rollback_isolates_subsequent_test(db_session) -> None:  # type: ignore[no-untyped-def]
    """The sentinel written by the previous test must not be visible here.

    If the fixture's rollback fired correctly, the lessons row with title
    ``_ISOLATION_SENTINEL`` was never committed and cannot be seen by this
    test's fresh transaction.  A non-zero count means rollback was skipped
    and isolation is broken.
    """
    count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM lessons WHERE title = :t"),
            {"t": _ISOLATION_SENTINEL},
        )
    ).scalar()
    assert count == 0
