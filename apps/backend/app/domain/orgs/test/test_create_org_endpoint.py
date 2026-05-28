"""Coverage for POST /api/orgs ().

Powers the `/orgs` picker page's "Create org" modal (E2a.19). The caller
becomes Admin of the new org. Slug must be lowercase a-z / 0-9 / hyphens
and unique.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.domain.orgs import org_settings_web as _org_settings_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"orgs"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="C")
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"user": user, "sess": sess}


@pytest.mark.asyncio
async def test_create_org_unauthenticated_returns_401() -> None:
    async with _client() as c:
        r = await c.post("/api/orgs", json={"name": "Acme", "slug": "acme"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_org_happy_path(seeded, db_session) -> None:
    sess = seeded["sess"]
    async with _client() as c:
        r = await c.post(
            "/api/orgs",
            json={"name": "Brand New", "slug": "brand-new"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["slug"] == "brand-new"
    assert body["name"] == "Brand New"
    assert body["role"] == "admin"
    # Membership row exists for the caller.
    membership = await orgs_repo.get_membership(db_session, user_id=seeded["user"].id, org_id=body["id"])
    assert membership is not None
    assert membership.role == "admin"


@pytest.mark.asyncio
async def test_create_org_invalid_slug_returns_422(seeded) -> None:
    sess = seeded["sess"]
    async with _client() as c:
        r = await c.post(
            "/api/orgs",
            json={"name": "X", "slug": "Has Spaces"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_org_duplicate_slug_returns_409(seeded, db_session) -> None:
    await orgs_repo.insert_org(db_session, slug="taken", display_name="Taken")
    await db_session.commit()
    sess = seeded["sess"]
    async with _client() as c:
        r = await c.post(
            "/api/orgs",
            json={"name": "Another", "slug": "taken"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 409
