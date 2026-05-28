"""Service-layer create ops for `core/identity` — round-trip tests against real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.identity import repository as repo
from app.core.identity import service as svc


@pytest.mark.asyncio
async def test_create_user_round_trip(db_session) -> None:
    user = await svc.create_user(db_session, display_name="Alice")
    assert user.id is not None
    assert user.display_name == "Alice"


@pytest.mark.asyncio
async def test_create_email_round_trip(db_session) -> None:
    user = await svc.create_user(db_session, display_name="Bob")
    email = await svc.create_email(
        db_session,
        user_id=user.id,
        email="bob@example.com",
        is_primary=True,
        verified=True,
    )
    assert email.user_id == user.id
    assert email.email == "bob@example.com"
    assert email.is_primary is True
    assert email.verified_at is not None


@pytest.mark.asyncio
async def test_create_oauth_identity_round_trip(db_session) -> None:
    user = await svc.create_user(db_session, display_name="Carol")
    identity = await svc.create_oauth_identity(
        db_session,
        user_id=user.id,
        provider="github",
        external_subject="gh-999",
        verified=True,
    )
    assert identity.user_id == user.id
    assert identity.provider == "github"
    assert identity.external_subject == "gh-999"
    assert identity.verified_at is not None


@pytest.mark.asyncio
async def test_create_session_round_trip(db_session) -> None:
    user = await svc.create_user(db_session, display_name="Dave")
    token_hash = repo.hash_token("raw-token-xyz")
    session = await svc.create_session(
        db_session,
        token_hash=token_hash,
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-test",
        ip=None,
        user_agent="test-agent",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert session.token_hash == token_hash
    assert session.user_id == user.id
    assert session.csrf_token == "csrf-test"
