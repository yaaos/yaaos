"""HTTP coverage for /api/auth/totp/enroll + verify."""

from __future__ import annotations

import httpx
import pyotp
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401


def _app() -> FastAPI:
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"sessions"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_enroll_without_session_returns_401() -> None:
    async with _client() as c:
        r = await c.post("/api/auth/totp/enroll")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_enroll_then_verify_happy_path(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="T")
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        enroll = await c.post("/api/auth/totp/enroll", cookies={"yaaos_session": s.raw_token})
        assert enroll.status_code == 200, enroll.text
        seed = enroll.json()["seed"]
        assert seed and enroll.json()["otpauth_uri"].startswith("otpauth://totp/")
        code = pyotp.TOTP(seed).now()
        verify = await c.post(
            "/api/auth/totp/verify",
            json={"code": code},
            cookies={"yaaos_session": s.raw_token},
        )
    assert verify.status_code == 200
    assert verify.json()["ok"] is True


@pytest.mark.asyncio
async def test_verify_wrong_code_returns_400(db_session) -> None:
    user = await identity_repo.insert_user(db_session)
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        await c.post("/api/auth/totp/enroll", cookies={"yaaos_session": s.raw_token})
        r = await c.post(
            "/api/auth/totp/verify",
            json={"code": "000000"},
            cookies={"yaaos_session": s.raw_token},
        )
    assert r.status_code == 400
    assert r.json()["error"] == "totp_invalid"
