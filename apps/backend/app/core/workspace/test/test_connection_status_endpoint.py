"""Service tests for GET /api/workspaces/connection_status.

Verifies auth enforcement and the happy-path status response.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401 — triggers auth.dep load
from app.core.workspace import web as _workspace_web  # noqa: F401 — registers /api/workspaces
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"workspaces"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    owner = await identity_repo.insert_user(db_session, display_name="Owner")
    builder = await identity_repo.insert_user(db_session, display_name="Builder")
    org = await orgs_repo.insert_org(db_session, slug="ws-status-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=builder.id, org_id=org.org_id, role=Role.BUILDER, handle="bld"
    )
    owner_sess = await session_lifecycle.create(db_session, user_id=owner.id, workspace_id=None)
    builder_sess = await session_lifecycle.create(db_session, user_id=builder.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "owner_sess": owner_sess,
        "builder_sess": builder_sess,
    }


@pytest.mark.asyncio
async def test_connection_status_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/workspaces/connection_status",
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_connection_status_builder_forbidden(seeded) -> None:
    """ORG_SETTINGS_READ requires ADMIN or OWNER; a BUILDER gets 403."""
    sess = seeded["builder_sess"]
    async with _client() as c:
        r = await c.get(
            "/api/workspaces/connection_status",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_connection_status_returns_not_configured(seeded) -> None:
    """No workspace-agent rows for this org → state is not_configured."""
    sess = seeded["owner_sess"]
    async with _client() as c:
        r = await c.get(
            "/api/workspaces/connection_status",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "not_configured"
