"""Coverage for `list_available()` + GET /api/plugins/available."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo
from app.domain.plugins import list_available
from app.domain.plugins import web as _plugins_web  # noqa: F401
from app.domain.sessions import web as _auth_web  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_plugins_registered() -> None:
    """Re-register plugins if a prior test cleared the registries."""
    from app.domain.coding_agent import registered_plugin_ids as _ca_ids  # noqa: PLC0415
    from app.domain.vcs import _PLUGINS as _V_PLUGINS  # noqa: PLC0415
    from app.plugins.claude_code import bootstrap as _cc_bootstrap  # noqa: PLC0415
    from app.plugins.github import bootstrap as _gh_bootstrap  # noqa: PLC0415

    if "claude_code" not in _ca_ids():
        _cc_bootstrap()
    if "github" not in _V_PLUGINS:
        _gh_bootstrap()


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"plugins"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="Picker User")
    org = await orgs_repo.insert_org(db_session, slug="picker-org")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.BUILDER, handle="pick"
    )
    session = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "session": session}


def test_list_available_vcs_returns_github() -> None:
    """The github plugin registers at import time. list_available('vcs') sees it."""
    metas = list_available("vcs")
    ids = [m.id for m in metas]
    assert "github" in ids
    github = next(m for m in metas if m.id == "github")
    assert github.type == "vcs"
    assert github.display_name


def test_list_available_coding_agent_returns_claude_code() -> None:
    metas = list_available("coding_agent")
    ids = [m.id for m in metas]
    assert "claude_code" in ids
    cc = next(m for m in metas if m.id == "claude_code")
    assert cc.type == "coding_agent"
    assert cc.display_name


def test_list_available_filters_workspace_to_empty() -> None:
    """Workspace plugins are infra — picker never lists them."""
    assert list_available("workspace") == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_endpoint_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/plugins/available",
            params={"type": "vcs"},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_member_can_read_vcs(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/plugins/available",
            params={"type": "vcs"},
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert any(p["id"] == "github" for p in payload["plugins"])


@pytest.mark.asyncio
async def test_endpoint_member_can_read_coding_agent(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/plugins/available",
            params={"type": "coding_agent"},
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert any(p["id"] == "claude_code" for p in payload["plugins"])
    # No VCS plugin in coding_agent list (type filter actually filters).
    assert not any(p["id"] == "github" for p in payload["plugins"])


@pytest.mark.asyncio
async def test_endpoint_rejects_bad_type(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/plugins/available",
            params={"type": "nope"},
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 422
