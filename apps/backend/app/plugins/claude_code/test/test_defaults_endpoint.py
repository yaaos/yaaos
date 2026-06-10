"""GET /api/claude_code/defaults — gated on CODING_AGENT_READ; returns
model / effort dropdown enums."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.plugins.claude_code import web as _cc_web  # noqa: F401


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"claude_code"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="cc-org")
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.org_id, role=Role.BUILDER, handle="mem"
    )
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "admin_sess": admin_sess, "member_sess": member_sess}


@pytest.mark.asyncio
async def test_defaults_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/claude_code/defaults", headers={"X-Yaaos-Org-Slug": seeded["org"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_defaults_member_forbidden(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/claude_code/defaults",
            cookies={"yaaos_session": seeded["member_sess"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_defaults_admin_gets_models_and_efforts(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/claude_code/defaults",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "models" in body
    assert "efforts" in body
    assert isinstance(body["models"], list)
    assert len(body["models"]) > 0
    # Orchestrator/agents no longer returned.
    assert "orchestrator" not in body
    assert "agents" not in body
