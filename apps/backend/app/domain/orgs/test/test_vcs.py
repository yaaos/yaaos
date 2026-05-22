"""Coverage for `domain/orgs.vcs` service + `/api/vcs` endpoints."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.core.audit_log import Actor
from app.core.audit_log.models import AuditEntryRow
from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import clear_vcs, get_vcs, set_vcs
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import vcs_web as _vcs_web  # noqa: F401
from app.domain.orgs.types import Role
from app.domain.sessions import web as _auth_web  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_github_registered() -> None:
    """Re-register the github plugin if a prior test cleared the registry."""
    from app.domain.vcs.registry import _PLUGINS  # noqa: PLC0415
    from app.plugins.github.service import bootstrap  # noqa: PLC0415

    if "github" not in _PLUGINS:
        bootstrap()


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    spec = _specs["vcs"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/vcs")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    owner = await identity_repo.insert_user(db_session, display_name="O")
    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="vcs-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.id, role=Role.MEMBER, handle="mem"
    )
    owner_sess = await session_lifecycle.create(db_session, user_id=owner.id, workspace_id=None)
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "owner": owner,
        "owner_sess": owner_sess,
        "admin_sess": admin_sess,
        "member_sess": member_sess,
    }


@pytest.mark.asyncio
async def test_set_vcs_service_persists_and_audits(seeded, db_session) -> None:
    org = seeded["org"]
    actor = Actor.user(user_id=seeded["owner"].id)
    state = await set_vcs(
        db_session,
        org_id=org.id,
        plugin_id="github",
        settings={"installation_id": 999},
        actor=actor,
    )
    assert state.plugin_id == "github"
    assert state.settings == {"installation_id": 999}

    reloaded = await get_vcs(db_session, org.id)
    assert reloaded.plugin_id == "github"
    assert reloaded.settings == {"installation_id": 999}

    rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.org_id == org.id, AuditEntryRow.kind == "vcs.installed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload == {"plugin_id": "github"}


@pytest.mark.asyncio
async def test_clear_vcs_removes_and_audits(seeded, db_session) -> None:
    org = seeded["org"]
    actor = Actor.user(user_id=seeded["owner"].id)
    await set_vcs(db_session, org_id=org.id, plugin_id="github", settings={}, actor=actor)
    removed = await clear_vcs(db_session, org_id=org.id, actor=actor)
    assert removed is True
    state = await get_vcs(db_session, org.id)
    assert state.plugin_id is None
    assert state.settings == {}

    rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.org_id == org.id, AuditEntryRow.kind == "vcs.cleared"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_clear_vcs_noop_returns_false_and_no_audit(seeded, db_session) -> None:
    org = seeded["org"]
    actor = Actor.user(user_id=seeded["owner"].id)
    removed = await clear_vcs(db_session, org_id=org.id, actor=actor)
    assert removed is False
    rows = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.org_id == org.id, AuditEntryRow.kind == "vcs.cleared"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_get_endpoint_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/vcs", headers={"X-Org-Slug": seeded["org"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_endpoint_member_forbidden(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/vcs",
            cookies={"yaaos_session": seeded["member_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_endpoint_returns_empty_state(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/vcs",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"plugin_id": None, "settings": {}}


@pytest.mark.asyncio
async def test_post_endpoint_github_returns_install_url(seeded) -> None:
    """github's `install_url(org_id)` is non-None, so POST returns it without
    persisting settings — the callback does that."""
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/vcs",
            json={"plugin_id": "github", "settings": {}},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["install_url"] == "/api/github/install"
    assert body["state"] is None


@pytest.mark.asyncio
async def test_post_endpoint_unknown_plugin_404(seeded) -> None:
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/vcs",
            json={"plugin_id": "ghost", "settings": {}},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_endpoint_clears_state(seeded, db_session) -> None:
    actor = Actor.user(user_id=seeded["owner"].id)
    await set_vcs(
        db_session,
        org_id=seeded["org"].id,
        plugin_id="github",
        settings={"installation_id": 1},
        actor=actor,
    )
    await db_session.commit()
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.delete(
            "/api/vcs",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"plugin_id": None, "settings": {}}
