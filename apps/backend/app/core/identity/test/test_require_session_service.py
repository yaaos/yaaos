"""Service tests for `require_session` — the session-only FastAPI dependency.

Verifies that `require_session` (owned by `core/identity`) resolves a valid
session cookie to `user_id_var`, and raises `AuthFailure` for missing, expired,
or unknown sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, register_handler
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.identity import user_web as _user_web  # noqa: F401  -- registers /api/user/*
from app.core.sessions import web as _auth_web  # noqa: F401  -- registers /api/auth/*


def _make_app() -> FastAPI:
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    register_handler(app)
    mount_specs(app, only={"user"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="Tester")
    await identity_repo.add_email(
        db_session, user_id=user.id, email="tester@x.test", is_primary=True, verified=True
    )
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"user": user, "session": s}


@pytest.mark.asyncio
async def test_require_session_resolves_user(seeded) -> None:
    """Valid session cookie → 200, user_id set, route returns body."""
    async with _client() as c:
        r = await c.get(
            "/api/user/me",
            cookies={"yaaos_session": seeded["session"].raw_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == str(seeded["user"].id)


@pytest.mark.asyncio
async def test_require_session_no_cookie_is_401(seeded) -> None:
    """Missing session cookie → 401 unauthenticated from `require_session`."""
    async with _client() as c:
        r = await c.get("/api/user/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_require_session_expired_cookie_is_401(db_session) -> None:
    """Expired session → 401 unauthenticated; no user resolution."""
    user = await identity_repo.insert_user(db_session, display_name="Exp")
    raw_token = "expired-token-xyz"
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw_token),
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-exp",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    await db_session.commit()

    async with _client() as c:
        r = await c.get(
            "/api/user/me",
            cookies={"yaaos_session": raw_token},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_require_session_unknown_token_is_401() -> None:
    """Token hash not in DB → 401 unauthenticated."""
    async with _client() as c:
        r = await c.get(
            "/api/user/me",
            cookies={"yaaos_session": "totally-unknown-token"},
        )
    assert r.status_code == 401
