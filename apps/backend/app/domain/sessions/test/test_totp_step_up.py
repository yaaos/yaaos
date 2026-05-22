"""Login step-up: when a user has verified TOTP and the IdP didn't
satisfy MFA, the OAuth callback must defer session creation and ask
for a TOTP code via /api/auth/totp/challenge."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import totp
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
async def user_with_totp(db_session):
    user = await identity_repo.insert_user(db_session, display_name="MFA")
    await identity_repo.add_email(db_session, user_id=user.id, email="mfa@example.com", verified=True)
    await identity_repo.add_oauth_identity(
        db_session, user_id=user.id, provider="test", external_subject="mfa-1"
    )
    seed, _ = await totp.enroll(db_session, user_id=user.id)
    # Promote to verified.
    code = pyotp.TOTP(seed).now()
    await totp.verify(db_session, user_id=user.id, code=code)
    await db_session.commit()
    yield {"user": user, "seed": seed}


@pytest.mark.asyncio
async def test_step_up_returns_challenge_when_mfa_not_satisfied(user_with_totp) -> None:
    set_next_profile(
        ProviderProfile(
            external_subject="mfa-1",
            primary_email="mfa@example.com",
            email_verified=True,
            display_name="MFA",
            mfa_satisfied=False,
        )
    )
    state = await _state()
    async with _client() as c:
        r = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
    assert r.status_code == 200
    assert r.json()["step_up"] == "totp_required"
    assert "yaaos_totp_challenge" in r.cookies


@pytest.mark.asyncio
async def test_mfa_satisfied_bypasses_step_up(user_with_totp) -> None:
    set_next_profile(
        ProviderProfile(
            external_subject="mfa-1",
            primary_email="mfa@example.com",
            email_verified=True,
            display_name="MFA",
            mfa_satisfied=True,
        )
    )
    state = await _state()
    async with _client() as c:
        r = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)
    assert "yaaos_session" in r.cookies


@pytest.mark.asyncio
async def test_totp_challenge_completes_login(user_with_totp) -> None:
    set_next_profile(
        ProviderProfile(
            external_subject="mfa-1",
            primary_email="mfa@example.com",
            email_verified=True,
            display_name="MFA",
            mfa_satisfied=False,
        )
    )
    state = await _state()
    async with _client() as c:
        first = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
        assert first.status_code == 200
        challenge_cookie = first.cookies["yaaos_totp_challenge"]
        code = pyotp.TOTP(user_with_totp["seed"]).now()
        complete = await c.post(
            "/api/auth/totp/challenge",
            json={"code": code},
            cookies={"yaaos_totp_challenge": challenge_cookie},
            follow_redirects=False,
        )
    assert complete.status_code in (302, 303)
    assert "yaaos_session" in complete.cookies
