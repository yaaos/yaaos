"""Model/effort defaults ride the coding-agents list, not a plugin route.

GET /api/claude_code/defaults returns 404; the defaults surface as
display_name/models/efforts fields on GET /api/coding-agents rows, and every
registered plugin is listed by GET /api/coding-agents/available.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.core.sessions  # noqa: F401  -- triggers auth route registration
from app.core.audit_log import Actor
from app.core.auth import AuthMiddleware, Role
from app.core.coding_agent import install_coding_agent, set_coding_agents_for_tests
from app.core.identity import create_user, mint_session
from app.domain.orgs import insert_membership, insert_org
from app.testing.fake_coding_agent import FakeCodingAgentPlugin


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"coding_agent"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    admin = await create_user(db_session, display_name="A")
    org = await insert_org(db_session, slug="cc-org")
    await insert_membership(db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm")
    admin_sess = await mint_session(db_session, user_id=admin.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "admin": admin, "admin_sess": admin_sess, "db_session": db_session}


@pytest.mark.asyncio
async def test_old_defaults_route_gone(seeded) -> None:
    """/api/claude_code/defaults must return 404 — route was removed."""
    async with _client() as c:
        r = await c.get(
            "/api/claude_code/defaults",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_coding_agents_list_carries_stage_options(seeded) -> None:
    """GET /api/coding-agents rows include display_name, models, efforts from the plugin."""
    fake = FakeCodingAgentPlugin(plugin_id="claude_code")
    from app.core.coding_agent import StageOptions  # noqa: PLC0415

    fake.display_name = "Claude Code"
    fake.stage_options = lambda: StageOptions(  # type: ignore[method-assign]
        models=("claude-sonnet-5",), efforts=("low", "high")
    )

    with set_coding_agents_for_tests() as reg:
        reg.replace(fake)  # type: ignore[arg-type]
        s = seeded["db_session"]
        actor = Actor.user(user_id=seeded["admin"].id)
        await install_coding_agent(
            s,
            org_id=seeded["org"].org_id,
            plugin_id="claude_code",
            settings={},
            actor=actor,
            created_by=seeded["admin"].id,
        )
        await s.commit()

        async with _client() as c:
            r = await c.get(
                "/api/coding-agents",
                cookies={"yaaos_session": seeded["admin_sess"].raw_token},
                headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 1
    row = body[0]
    assert row["plugin_id"] == "claude_code"
    assert row["display_name"] == "Claude Code"
    assert row["models"] == ["claude-sonnet-5"]
    assert row["efforts"] == ["low", "high"]


@pytest.mark.asyncio
async def test_available_endpoint_lists_registered_plugins(seeded) -> None:
    """GET /api/coding-agents/available returns all registered plugins."""
    fake = FakeCodingAgentPlugin(plugin_id="claude_code")
    fake.display_name = "Claude Code"

    with set_coding_agents_for_tests() as reg:
        reg.replace(fake)  # type: ignore[arg-type]
        async with _client() as c:
            r = await c.get(
                "/api/coding-agents/available",
                cookies={"yaaos_session": seeded["admin_sess"].raw_token},
                headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert "plugins" in body
    assert any(p["plugin_id"] == "claude_code" for p in body["plugins"])
    plugin = next(p for p in body["plugins"] if p["plugin_id"] == "claude_code")
    assert plugin["display_name"] == "Claude Code"
