"""Lifecycle tests for `core/identity.sessions`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.identity import repository as repo
from app.core.identity import sessions
from app.core.identity.types import SessionNotFoundError


@pytest.mark.asyncio
async def test_create_and_lookup(db_session) -> None:
    user = await repo.insert_user(db_session)
    result = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    assert result.raw_token
    assert result.csrf_token
    assert result.session.user_id == user.id

    found = await sessions.lookup(db_session, result.raw_token)
    assert found is not None
    assert found.user_id == user.id


@pytest.mark.asyncio
async def test_create_requires_exactly_one_principal(db_session) -> None:
    with pytest.raises(ValueError):
        await sessions.create(db_session, user_id=None, workspace_id=None)
    with pytest.raises(ValueError):
        from uuid import uuid4  # noqa: PLC0415

        await sessions.create(db_session, user_id=uuid4(), workspace_id=uuid4())


@pytest.mark.asyncio
async def test_lookup_expired_returns_none(db_session) -> None:
    user = await repo.insert_user(db_session)
    result = await sessions.create(
        db_session, user_id=user.id, workspace_id=None, lifetime=timedelta(milliseconds=1)
    )
    # Force the row's expires_at into the past so we don't sleep in the test.
    from sqlalchemy import update  # noqa: PLC0415

    from app.core.identity.models import SessionRow  # noqa: PLC0415

    await db_session.execute(
        update(SessionRow)
        .where(SessionRow.token_hash == sessions._hash(result.raw_token))
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    found = await sessions.lookup(db_session, result.raw_token)
    assert found is None


@pytest.mark.asyncio
async def test_rotate_invalidates_old_token(db_session) -> None:
    user = await repo.insert_user(db_session)
    first = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    rotated = await sessions.rotate(db_session, first.raw_token)
    assert rotated.raw_token != first.raw_token
    assert rotated.csrf_token != first.csrf_token

    assert await sessions.lookup(db_session, first.raw_token) is None
    assert await sessions.lookup(db_session, rotated.raw_token) is not None


@pytest.mark.asyncio
async def test_rotate_unknown_token_raises(db_session) -> None:
    with pytest.raises(SessionNotFoundError):
        await sessions.rotate(db_session, "no-such-token")


@pytest.mark.asyncio
async def test_revoke_one(db_session) -> None:
    user = await repo.insert_user(db_session)
    s = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    await sessions.revoke(db_session, s.raw_token)
    assert await sessions.lookup(db_session, s.raw_token) is None


@pytest.mark.asyncio
async def test_revoke_all_for_user(db_session) -> None:
    user = await repo.insert_user(db_session)
    a = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    b = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    n = await sessions.revoke_all_for_user(db_session, user.id)
    assert n == 2
    assert await sessions.lookup(db_session, a.raw_token) is None
    assert await sessions.lookup(db_session, b.raw_token) is None


@pytest.mark.asyncio
async def test_mark_sso_satisfied_and_is_sso_satisfied(db_session) -> None:
    from uuid import uuid4  # noqa: PLC0415

    user = await repo.insert_user(db_session)
    s = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    org_id = uuid4()
    updated = await sessions.mark_sso_satisfied(db_session, s.raw_token, org_id=org_id)
    assert sessions.is_sso_satisfied(updated, org_id=org_id)
    # Different org → not satisfied.
    assert not sessions.is_sso_satisfied(updated, org_id=uuid4())


@pytest.mark.asyncio
async def test_sso_satisfaction_expires_after_8h(db_session) -> None:
    from uuid import uuid4  # noqa: PLC0415

    user = await repo.insert_user(db_session)
    s = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    org_id = uuid4()
    updated = await sessions.mark_sso_satisfied(db_session, s.raw_token, org_id=org_id)
    # Force the satisfied_at row 9 hours into the past.
    stale = updated.model_copy(update={"sso_satisfied_at": datetime.now(UTC) - timedelta(hours=9)})
    assert not sessions.is_sso_satisfied(stale, org_id=org_id)


@pytest.mark.asyncio
async def test_cleanup_expired(db_session) -> None:
    user = await repo.insert_user(db_session)
    alive = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    expired = await sessions.create(db_session, user_id=user.id, workspace_id=None)
    from sqlalchemy import update  # noqa: PLC0415

    from app.core.identity.models import SessionRow  # noqa: PLC0415

    await db_session.execute(
        update(SessionRow)
        .where(SessionRow.token_hash == sessions._hash(expired.raw_token))
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    n = await sessions.cleanup_expired(db_session)
    assert n == 1
    assert await sessions.lookup(db_session, alive.raw_token) is not None
    assert await sessions.lookup(db_session, expired.raw_token) is None
