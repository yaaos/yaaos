"""HTTP coverage for /api/byok — list, set, validate, delete with role gating."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core import byok as byok_service
from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import byok_routes as _byok_routes  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role
from app.domain.sessions import web as _auth_web  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_anthropic_validator() -> None:
    async def _ok(_: str) -> bool:
        return True

    byok_service.register_validator("anthropic", _ok)


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    spec = _specs["byok"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/byok")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="byok-ep-org")
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.id, role=Role.MEMBER, handle="mem"
    )
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "admin_sess": admin_sess, "member_sess": member_sess}


@pytest.mark.asyncio
async def test_list_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/byok", headers={"X-Org-Slug": seeded["org"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_member_forbidden(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/byok",
            cookies={"yaaos_session": seeded["member_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_admin_sees_not_set_initially(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/byok",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    anthropic = next(p for p in body if p["provider"] == "anthropic")
    assert anthropic["status"] == "not_set"


@pytest.mark.asyncio
async def test_set_validate_clear_round_trip(seeded) -> None:
    sess = seeded["admin_sess"]
    headers = {"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token}
    cookies = {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token}
    async with _client() as c:
        # Set.
        r = await c.post(
            "/api/byok/anthropic",
            json={"value": "sk-test"},
            cookies=cookies,
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "configured"}

        # Validate (stub returns True).
        r = await c.post(
            "/api/byok/anthropic/validate",
            cookies=cookies,
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"valid": True}

        # Clear.
        r = await c.delete(
            "/api/byok/anthropic",
            cookies=cookies,
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"removed": True}


@pytest.mark.asyncio
async def test_set_rejects_empty(seeded) -> None:
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/byok/anthropic",
            json={"value": ""},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_unknown_provider_404(seeded) -> None:
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/byok/ghost",
            json={"value": "x"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 404
