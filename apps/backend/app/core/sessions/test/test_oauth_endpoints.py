"""End-to-end /api/auth/login + /api/auth/callback driven through ASGI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.identity import ProviderProfile
from app.core.identity import repository as repo
from app.core.sessions import web as auth_web  # noqa: F401 — ensures /api/auth routes register
from app.domain.orgs import InvitationRow, Role
from app.domain.orgs import repository as orgs_repo
from app.plugins.oauth_test import set_next_profile


def _app() -> FastAPI:
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"sessions"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
    )


def _good_state() -> str:
    """Build a valid state string by calling /api/auth/login first."""
    raise NotImplementedError


@pytest.mark.asyncio
async def test_login_redirects_to_provider() -> None:
    async with _client() as c:
        resp = await c.get("/api/auth/login", params={"provider": "test"})
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert "code=test-code" in location
    assert "state=" in location


@pytest.mark.asyncio
async def test_login_unknown_provider_returns_404() -> None:
    async with _client() as c:
        resp = await c.get("/api/auth/login", params={"provider": "nope"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_provider"


async def _begin_login_and_get_state() -> str:
    async with _client() as c:
        resp = await c.get("/api/auth/login", params={"provider": "test", "next": "/orgs/x/dashboard"})
    parts = urlparse(resp.headers["location"])
    return parse_qs(parts.query)["state"][0]


@pytest.mark.asyncio
async def test_callback_existing_identity_issues_session(db_session) -> None:
    user = await repo.insert_user(db_session, display_name="E")
    await repo.add_email(db_session, user_id=user.id, email="e@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="test", external_subject="ex-1")
    state = await _begin_login_and_get_state()
    set_next_profile(
        ProviderProfile(
            external_subject="ex-1",
            primary_email="e@example.com",
            email_verified=True,
            display_name="E",
        )
    )

    async with _client() as c:
        resp = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    assert "yaaos_session" in resp.cookies
    assert "yaaos_csrf" in resp.cookies


@pytest.mark.asyncio
async def test_callback_unknown_user_redirects_to_login_with_reason(db_session) -> None:
    """OAuth never provisions: unknown `(provider, external_subject)` + unknown
    primary email → redirect to `/login?reason=not_provisioned` with NO cookie
    set and NO rows created. The user must be invited first."""
    state = await _begin_login_and_get_state()
    set_next_profile(
        ProviderProfile(
            external_subject="ex-2",
            primary_email="nobody@example.com",
            email_verified=True,
            display_name="Nobody",
        )
    )

    async with _client() as c:
        resp = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/login?reason=not_provisioned"
    assert "yaaos_session" not in resp.cookies
    # No rows were written.
    assert await repo.find_user_by_email(db_session, "nobody@example.com") is None
    assert await repo.find_oauth_identity(db_session, provider="test", external_subject="ex-2") is None


@pytest.mark.asyncio
async def test_callback_email_match_without_identity_autolinks(db_session) -> None:
    user = await repo.insert_user(db_session)
    await repo.add_email(db_session, user_id=user.id, email="dup@example.com", verified=True)
    await repo.add_oauth_identity(db_session, user_id=user.id, provider="other", external_subject="o-1")
    await db_session.commit()
    state = await _begin_login_and_get_state()
    set_next_profile(
        ProviderProfile(
            external_subject="ex-3",
            primary_email="dup@example.com",
            email_verified=True,
            display_name="Dup",
        )
    )

    async with _client() as c:
        resp = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    assert "yaaos_session" in resp.cookies
    # Identity row attached to the pre-existing user, no new user row.
    linked = await repo.find_oauth_identity(db_session, provider="test", external_subject="ex-3")
    assert linked is not None and linked.user_id == user.id


@pytest.mark.asyncio
async def test_callback_email_not_verified_returns_403(db_session) -> None:
    state = await _begin_login_and_get_state()
    set_next_profile(
        ProviderProfile(
            external_subject="ex-4",
            primary_email="unv@example.com",
            email_verified=False,
            display_name="Unv",
        )
    )

    async with _client() as c:
        resp = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "email_not_verified"


@pytest.mark.asyncio
async def test_callback_invitation_alone_does_not_provision(db_session) -> None:
    """A pending invitation for a stranger's email is no longer enough to sign
    them in — OAuth never creates users. The invitation must be explicitly
    accepted via `/api/memberships/accept` (which creates the user if needed).
    Here we assert the legacy "invitation-on-first-login" pathway is gone."""
    org = await orgs_repo.insert_org(db_session, slug="inviteorg")
    db_session.add(
        InvitationRow(
            id=uuid4(),
            org_id=org.id,
            email="newbie@example.com",
            role=Role.BUILDER.value,
            token_hash="z" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            invited_by_user_id=None,
        )
    )
    await db_session.flush()
    await db_session.commit()

    state = await _begin_login_and_get_state()
    set_next_profile(
        ProviderProfile(
            external_subject="ex-5",
            primary_email="newbie@example.com",
            email_verified=True,
            display_name="Newbie",
        )
    )

    async with _client() as c:
        resp = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/login?reason=not_provisioned"
    assert "yaaos_session" not in resp.cookies


@pytest.mark.asyncio
async def test_callback_invalid_state_returns_400() -> None:
    async with _client() as c:
        resp = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": "not-a-signed-value"},
            follow_redirects=False,
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_logout_clears_cookies() -> None:
    async with _client() as c:
        resp = await c.post(
            "/api/auth/logout",
            cookies={"yaaos_session": "nonexistent-token"},
        )
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert "yaaos_session=" in set_cookie
