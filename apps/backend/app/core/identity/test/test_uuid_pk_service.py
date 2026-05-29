"""Service test: inserting a row without an explicit PK yields a DB-minted UUID v7.

Exercises the `uuidv7()` server_default on `users.id` against a real Postgres
instance. Validates that the DB generates version-7 UUIDs when callers omit
the `id=` kwarg from the Row constructor.
"""

from __future__ import annotations

import pytest

from app.core.identity import repository as repo


@pytest.mark.asyncio
@pytest.mark.service
async def test_insert_without_id_yields_v7_uuid(db_session) -> None:
    """Row inserted without an explicit PK carries a DB-minted UUID v7."""
    row = await repo.insert_user(db_session, display_name="v7-test")
    # The DB flush inside insert_user populates row.id from uuidv7().
    assert row.id is not None
    assert row.id.version == 7
