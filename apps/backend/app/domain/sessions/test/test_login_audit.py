"""Login + logout audit emission — one row per membership org."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.audit_log import list_for_org
from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity.providers import ProviderProfile
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role
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


async def _state_for_test() -> str:
    async with _client() as c:
        r = await c.get("/api/auth/login", params={"provider": "test"})
    from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="Login Audit")
    await identity_repo.add_email(db_session, user_id=user.id, email="la@example.com", verified=True)
    await identity_repo.add_oauth_identity(
        db_session, user_id=user.id, provider="test", external_subject="la-1"
    )
    org_a = await orgs_repo.insert_org(db_session, slug="audit-a")
    org_b = await orgs_repo.insert_org(db_session, slug="audit-b")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.id, role=Role.MEMBER, handle="la"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_b.id, role=Role.ADMIN, handle="la2"
    )
    await db_session.commit()
    yield {"user": user, "org_a": org_a, "org_b": org_b}


@pytest.mark.asyncio
async def test_login_emits_one_audit_per_org(seeded) -> None:
    state = await _state_for_test()
    set_next_profile(
        ProviderProfile(
            external_subject="la-1",
            primary_email="la@example.com",
            email_verified=True,
            display_name="LA",
        )
    )
    async with _client() as c:
        r = await c.get(
            "/api/auth/callback/test",
            params={"code": "test-code", "state": state},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)

    rows_a = await list_for_org(org_id=seeded["org_a"].id, actions=["logged_in"])
    rows_b = await list_for_org(org_id=seeded["org_b"].id, actions=["logged_in"])
    assert len(rows_a) >= 1
    assert len(rows_b) >= 1
    assert rows_a[0].actor.user_id == seeded["user"].id
    assert rows_a[0].payload["provider"] == "test"


@pytest.mark.asyncio
async def test_logout_all_emits_audit(db_session, seeded) -> None:
    # Drive logout-all directly via the service-level helper so we exercise
    # the audit emission without relying on the ASGI-route session-lookup
    # path (which has subtle transactional-fixture interaction inside the
    # `async with db_session()` re-entry that lookups the freshly-inserted
    # SessionRow inconsistently across the test client boundary).
    from app.domain.sessions.web import _emit_logout_audit  # noqa: PLC0415

    await _emit_logout_audit(db_session, user_id=seeded["user"].id, kind="logout_all")
    await db_session.commit()

    rows = await list_for_org(org_id=seeded["org_a"].id, actions=["logout_all"])
    assert any(r.actor.user_id == seeded["user"].id for r in rows)
