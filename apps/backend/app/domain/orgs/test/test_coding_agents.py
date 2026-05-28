"""Coverage for `domain/orgs.coding_agents` service + `/api/coding-agents` endpoints."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.audit_log import Actor, list_for_org
from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import (
    CodingAgentAlreadyInstalledError,
    CodingAgentNotInstalledError,
    install_coding_agent,
    list_coding_agents,
    uninstall_coding_agent,
    update_coding_agent_settings,
)
from app.domain.orgs import (
    coding_agents_web as _ca_web,  # noqa: F401
)
from app.domain.orgs import (
    repository as orgs_repo,
)
from app.domain.orgs.types import Role
from app.domain.sessions import web as _auth_web  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_claude_code_registered() -> None:
    from app.domain.coding_agent import registered_plugin_ids  # noqa: PLC0415
    from app.plugins.claude_code import bootstrap  # noqa: PLC0415

    if "claude_code" not in registered_plugin_ids():
        bootstrap()


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"coding_agents"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    owner = await identity_repo.insert_user(db_session, display_name="O")
    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="ca-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.id, role=Role.BUILDER, handle="mem"
    )
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "owner": owner,
        "admin": admin,
        "admin_sess": admin_sess,
        "member_sess": member_sess,
    }


@pytest.mark.asyncio
async def test_install_and_list(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    install = await install_coding_agent(
        db_session,
        org_id=seeded["org"].id,
        plugin_id="claude_code",
        settings={"orchestrator": {}, "agents": []},
        actor=actor,
        created_by=seeded["owner"].id,
    )
    assert install.plugin_id == "claude_code"
    rows = await list_coding_agents(db_session, seeded["org"].id)
    assert len(rows) == 1
    assert rows[0].plugin_id == "claude_code"
    assert rows[0].created_by == seeded["owner"].id


@pytest.mark.asyncio
async def test_install_emits_audit(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    await install_coding_agent(
        db_session,
        org_id=seeded["org"].id,
        plugin_id="claude_code",
        settings={},
        actor=actor,
    )
    rows = await list_for_org(org_id=seeded["org"].id, actions=["coding_agent.installed"])
    assert len(rows) == 1
    assert rows[0].payload == {"plugin_id": "claude_code"}


@pytest.mark.asyncio
async def test_install_twice_raises(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    await install_coding_agent(
        db_session, org_id=seeded["org"].id, plugin_id="claude_code", settings={}, actor=actor
    )
    with pytest.raises(CodingAgentAlreadyInstalledError):
        await install_coding_agent(
            db_session,
            org_id=seeded["org"].id,
            plugin_id="claude_code",
            settings={},
            actor=actor,
        )


@pytest.mark.asyncio
async def test_update_settings_and_audit(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    await install_coding_agent(
        db_session,
        org_id=seeded["org"].id,
        plugin_id="claude_code",
        settings={"orchestrator": {"name": "old"}, "agents": []},
        actor=actor,
    )
    updated = await update_coding_agent_settings(
        db_session,
        org_id=seeded["org"].id,
        plugin_id="claude_code",
        settings={"orchestrator": {"name": "new"}, "agents": []},
        actor=actor,
    )
    assert updated.settings == {"orchestrator": {"name": "new"}, "agents": []}
    rows = await list_for_org(org_id=seeded["org"].id, actions=["coding_agent.settings_updated"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_update_missing_install_raises(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    with pytest.raises(CodingAgentNotInstalledError):
        await update_coding_agent_settings(
            db_session,
            org_id=seeded["org"].id,
            plugin_id="claude_code",
            settings={},
            actor=actor,
        )


@pytest.mark.asyncio
async def test_uninstall_returns_true_and_audits(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    await install_coding_agent(
        db_session, org_id=seeded["org"].id, plugin_id="claude_code", settings={}, actor=actor
    )
    removed = await uninstall_coding_agent(
        db_session, org_id=seeded["org"].id, plugin_id="claude_code", actor=actor
    )
    assert removed is True
    rows = await list_for_org(org_id=seeded["org"].id, actions=["coding_agent.uninstalled"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_uninstall_noop_returns_false_no_audit(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    removed = await uninstall_coding_agent(
        db_session, org_id=seeded["org"].id, plugin_id="claude_code", actor=actor
    )
    assert removed is False


@pytest.mark.asyncio
async def test_endpoint_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/coding-agents", headers={"X-Org-Slug": seeded["org"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_member_forbidden(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/coding-agents",
            cookies={"yaaos_session": seeded["member_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_install_and_list_via_http(seeded) -> None:
    async with _client() as c:
        # Empty settings → claude_code plugin substitutes defaults.
        r = await c.post(
            "/api/coding-agents",
            json={"plugin_id": "claude_code", "settings": {}},
            cookies={
                "yaaos_session": seeded["admin_sess"].raw_token,
                "yaaos_csrf": seeded["admin_sess"].csrf_token,
            },
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["admin_sess"].csrf_token},
        )
        assert r.status_code == 200, r.text
        listing = await c.get(
            "/api/coding-agents",
            cookies={
                "yaaos_session": seeded["admin_sess"].raw_token,
                "yaaos_csrf": seeded["admin_sess"].csrf_token,
            },
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["admin_sess"].csrf_token},
        )
    assert listing.status_code == 200
    body = listing.json()
    assert len(body) == 1
    assert body[0]["plugin_id"] == "claude_code"


@pytest.mark.asyncio
async def test_endpoint_rejects_invalid_settings(seeded) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/coding-agents",
            json={"plugin_id": "claude_code", "settings": {"rogue": "value"}},
            cookies={
                "yaaos_session": seeded["admin_sess"].raw_token,
                "yaaos_csrf": seeded["admin_sess"].csrf_token,
            },
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["admin_sess"].csrf_token},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_endpoint_duplicate_install_409(seeded) -> None:
    async with _client() as c:
        await c.post(
            "/api/coding-agents",
            json={"plugin_id": "claude_code", "settings": {}},
            cookies={
                "yaaos_session": seeded["admin_sess"].raw_token,
                "yaaos_csrf": seeded["admin_sess"].csrf_token,
            },
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["admin_sess"].csrf_token},
        )
        r = await c.post(
            "/api/coding-agents",
            json={"plugin_id": "claude_code", "settings": {}},
            cookies={
                "yaaos_session": seeded["admin_sess"].raw_token,
                "yaaos_csrf": seeded["admin_sess"].csrf_token,
            },
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["admin_sess"].csrf_token},
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_endpoint_uninstall_404_when_missing(seeded) -> None:
    async with _client() as c:
        r = await c.delete(
            "/api/coding-agents/claude_code",
            cookies={
                "yaaos_session": seeded["admin_sess"].raw_token,
                "yaaos_csrf": seeded["admin_sess"].csrf_token,
            },
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["admin_sess"].csrf_token},
        )
    assert r.status_code == 404
