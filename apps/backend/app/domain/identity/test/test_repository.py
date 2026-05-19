"""Repository-level smoke tests for `domain/identity` against real Postgres.

These tests exercise the migration + raw inserts. They establish that every
M02 table the architecture document declared actually exists with the
expected columns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.identity import repository as repo


@pytest.mark.asyncio
async def test_create_user_with_email_and_oauth_identity(db_session) -> None:
    user = await repo.insert_user(db_session, display_name="Jack")
    assert user.id is not None
    assert user.display_name == "Jack"

    email_row = await repo.add_email(
        db_session,
        user_id=user.id,
        email="jack@example.com",
        is_primary=True,
        verified=True,
    )
    assert email_row.verified_at is not None
    assert email_row.is_primary is True

    oauth = await repo.add_oauth_identity(
        db_session,
        user_id=user.id,
        provider="github",
        external_subject="42",
    )
    assert oauth.verified_at is not None

    looked_up = await repo.find_oauth_identity(db_session, provider="github", external_subject="42")
    assert looked_up is not None
    assert looked_up.user_id == user.id


@pytest.mark.asyncio
async def test_find_user_by_email_case_insensitive(db_session) -> None:
    user = await repo.insert_user(db_session)
    await repo.add_email(db_session, user_id=user.id, email="Mixed.Case@Example.com", verified=True)
    hit = await repo.find_user_by_email(db_session, "mixed.case@example.com")
    assert hit is not None and hit.id == user.id


@pytest.mark.asyncio
async def test_find_user_by_unverified_email_returns_none(db_session) -> None:
    user = await repo.insert_user(db_session)
    await repo.add_email(db_session, user_id=user.id, email="x@example.com", verified=False)
    assert await repo.find_user_by_email(db_session, "x@example.com") is None


@pytest.mark.asyncio
async def test_session_insert_and_lookup(db_session) -> None:
    user = await repo.insert_user(db_session)
    token_hash = repo.hash_token("rawtoken")
    row = await repo.insert_session(
        db_session,
        token_hash=token_hash,
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-abc",
        ip="127.0.0.1",
        user_agent="pytest",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert row.token_hash == token_hash

    fetched = await repo.get_session_by_hash(db_session, token_hash)
    assert fetched is not None and fetched.user_id == user.id
    assert fetched.csrf_token == "csrf-abc"


@pytest.mark.asyncio
async def test_totp_secret_upsert(db_session) -> None:
    user = await repo.insert_user(db_session)
    first = await repo.upsert_totp_secret(db_session, user_id=user.id, encrypted_secret=b"sec1")
    assert first.verified_at is None

    # Mark it verified, then upsert again — verification should reset.
    first.verified_at = datetime.now(UTC)
    await db_session.flush()
    second = await repo.upsert_totp_secret(db_session, user_id=user.id, encrypted_secret=b"sec2")
    assert second.encrypted_secret == b"sec2"
    assert second.verified_at is None


@pytest.mark.asyncio
async def test_github_installation_upsert(db_session) -> None:
    org_id = uuid4()
    await repo.upsert_github_installation(db_session, installation_id=123, org_id=org_id)
    found = await repo.find_installation_org(db_session, installation_id=123)
    assert found == org_id

    # Re-pointing the installation updates the row in place.
    new_org = uuid4()
    await repo.upsert_github_installation(db_session, installation_id=123, org_id=new_org)
    assert await repo.find_installation_org(db_session, installation_id=123) == new_org
