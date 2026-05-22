"""Session rotation on login: the pre-auth session cookie must be revoked
when a fresh authed session is minted. Spec §"Sessions": rotated on
login, SSO satisfaction, role change."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.identity.providers import ProviderProfile
from app.domain.sessions import web as _auth_web  # noqa: F401
from app.plugins.oauth_test import set_next_profile


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    spec = _specs["sessions"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/auth")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _state() -> str:
    async with _client() as c:
        r = await c.get("/api/auth/login", params={"provider": "test"})
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


@pytest_asyncio.fixture
async def staged_user(db_session):
    user = await identity_repo.insert_user(db_session, display_name="Rot")
    await identity_repo.add_email(db_session, user_id=user.id, email="rot@example.com", verified=True)
    await identity_repo.add_oauth_identity(
        db_session, user_id=user.id, provider="test", external_subject="rot-1"
    )
    pre = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"user": user, "pre": pre}


@pytest.mark.asyncio
async def test_pre_auth_cookie_revoked_on_callback(staged_user, db_session) -> None:
    """Existing session cookie at callback time is revoked; only the
    freshly-minted session survives."""
    state = await _state()
    set_next_profile(
        ProviderProfile(
            external_subject="rot-1",
            primary_email="rot@example.com",
            email_verified=True,
            display_name="Rot",
            mfa_satisfied=True,
        )
    )
    pre_token = staged_user["pre"].raw_token

    async with _client() as c:
        r = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            cookies={"yaaos_session": pre_token},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)

    # The pre-auth token is gone; the new token (in the Set-Cookie header) lives.
    from app.core.database import session as factory  # noqa: PLC0415

    async with factory() as s:
        assert await session_lifecycle.lookup(s, pre_token) is None

    new_token = r.cookies.get("yaaos_session")
    assert new_token is not None and new_token != pre_token
    async with factory() as s:
        assert await session_lifecycle.lookup(s, new_token) is not None
