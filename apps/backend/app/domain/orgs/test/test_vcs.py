"""Coverage for `domain/orgs.vcs` service + `/api/vcs` endpoints."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.audit_log import Actor, list_for_org
from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401
from app.domain.orgs import clear_vcs, get_vcs, set_vcs
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import vcs_web as _vcs_web  # noqa: F401


@pytest.fixture(autouse=True)
def _ensure_github_registered() -> None:
    """Re-register the github plugin if a prior test cleared the registry."""
    from app.core.vcs import is_registered  # noqa: PLC0415
    from app.plugins.github import bootstrap  # noqa: PLC0415

    if not is_registered("github"):
        bootstrap()


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"vcs"})
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
        db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.org_id, role=Role.BUILDER, handle="mem"
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
        org_id=org.org_id,
        plugin_id="github",
        settings={"installation_id": 999},
        actor=actor,
    )
    assert state.plugin_id == "github"
    assert state.settings == {"installation_id": 999}

    reloaded = await get_vcs(db_session, org.org_id)
    assert reloaded.plugin_id == "github"
    assert reloaded.settings == {"installation_id": 999}

    rows = await list_for_org(org_id=org.org_id, actions=["vcs.installed"])
    assert len(rows) == 1
    assert rows[0].payload == {"plugin_id": "github"}


@pytest.mark.asyncio
async def test_clear_vcs_removes_and_audits(seeded, db_session) -> None:
    org = seeded["org"]
    actor = Actor.user(user_id=seeded["owner"].id)
    await set_vcs(db_session, org_id=org.org_id, plugin_id="github", settings={}, actor=actor)
    removed = await clear_vcs(db_session, org_id=org.org_id, actor=actor)
    assert removed is True
    state = await get_vcs(db_session, org.org_id)
    assert state.plugin_id is None
    assert state.settings == {}

    rows = await list_for_org(org_id=org.org_id, actions=["vcs.cleared"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_clear_vcs_noop_returns_false_and_no_audit(seeded, db_session) -> None:
    org = seeded["org"]
    actor = Actor.user(user_id=seeded["owner"].id)
    removed = await clear_vcs(db_session, org_id=org.org_id, actor=actor)
    assert removed is False
    rows = await list_for_org(org_id=org.org_id, actions=["vcs.cleared"])
    assert rows == []


@pytest.mark.asyncio
async def test_get_endpoint_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/vcs", headers={"X-Yaaos-Org-Slug": seeded["org"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_get_endpoint_member_forbidden(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/vcs",
            cookies={"yaaos_session": seeded["member_sess"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_get_endpoint_returns_empty_state(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/vcs",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"plugin_id": None, "settings": {}}


@pytest.mark.asyncio
async def test_post_endpoint_github_writes_state(seeded) -> None:
    """github's `install_url(org_id)` is None — the install handshake is
    driven separately by `POST /api/github/install/start`. So POST /api/vcs
    just records the picker choice."""
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/vcs",
            json={"plugin_id": "github", "settings": {}},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["install_url"] is None
    assert body["state"] == {"plugin_id": "github", "settings": {}}


@pytest.mark.asyncio
async def test_post_endpoint_unknown_plugin_404(seeded) -> None:
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/vcs",
            json={"plugin_id": "ghost", "settings": {}},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_endpoint_clears_state(seeded, db_session) -> None:
    """Remove fully disconnects: nulls the org's vcs_* columns AND wipes the
    github plugin's credentials + install rows. The next Add starts blank."""
    from app.plugins.github import record_app_install  # noqa: PLC0415

    actor = Actor.user(user_id=seeded["owner"].id)
    await set_vcs(
        db_session,
        org_id=seeded["org"].org_id,
        plugin_id="github",
        settings={"installation_id": 1},
        actor=actor,
    )
    # Seed the per-org install row Remove should wipe. Platform App
    # credentials live in env vars, so there's no per-org settings row.
    await record_app_install(
        db_session,
        org_id=seeded["org"].org_id,
        install_external_id="9999",
        account_login="acme-org",
    )
    await db_session.commit()

    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.delete(
            "/api/vcs",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"plugin_id": None, "settings": {}}

    # The install row is gone for this org — verify via raw SQL.
    from sqlalchemy import text as _text  # noqa: PLC0415

    from app.core.database import session as _db_session_factory  # noqa: PLC0415

    async with _db_session_factory() as s:
        count = (
            await s.execute(
                _text("SELECT COUNT(*) FROM github_app_installations WHERE org_id = :oid"),
                {"oid": seeded["org"].org_id},
            )
        ).scalar_one()
    assert count == 0
