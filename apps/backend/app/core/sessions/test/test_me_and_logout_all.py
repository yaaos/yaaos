"""Coverage for `/api/auth/me` and `/api/auth/logout-all`."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"sessions"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_me_without_session_returns_401() -> None:
    async with _client() as c:
        resp = await c.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_user_and_memberships(db_session) -> None:
    user = await identity_repo.insert_user(db_session, display_name="Jack K")
    await identity_repo.add_email(
        db_session, user_id=user.id, email="jack@example.com", is_primary=True, verified=True
    )
    org_a = await orgs_repo.insert_org(db_session, slug="me-org-a", display_name="A")
    org_b = await orgs_repo.insert_org(db_session, slug="me-org-b", display_name="B")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.id, role=Role.OWNER, handle="jack"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_b.id, role=Role.ADMIN, handle="jk"
    )
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        resp = await c.get("/api/auth/me", cookies={"yaaos_session": s.raw_token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["display_name"] == "Jack K"
    assert body["user"]["primary_email"] == "jack@example.com"
    slugs = sorted(m["slug"] for m in body["memberships"])
    assert slugs == ["me-org-a", "me-org-b"]
    # Server has no opinion about which org is "current" — that's view state
    # and lives in the URL. The response shape carries no current_org_slug.
    assert "current_org_slug" not in body

    # Cleanup so other tests start clean.
    from sqlalchemy import delete  # noqa: PLC0415

    from app.core.database import get_sessionmaker  # noqa: PLC0415
    from app.core.identity import _delete_user_artifacts_for_tests  # noqa: PLC0415
    from app.domain.orgs import MembershipRow, OrgRow  # noqa: PLC0415

    async with get_sessionmaker()() as cleanup:
        await cleanup.execute(delete(MembershipRow).where(MembershipRow.user_id == user.id))
        await cleanup.execute(delete(OrgRow).where(OrgRow.id.in_([org_a.id, org_b.id])))
        await _delete_user_artifacts_for_tests(cleanup, user_id=user.id)
        await cleanup.commit()


@pytest.mark.asyncio
async def test_logout_all_revokes_every_session(db_session) -> None:
    user = await identity_repo.insert_user(db_session)
    s1 = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    s2 = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    s3 = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        resp = await c.post("/api/auth/logout-all", cookies={"yaaos_session": s1.raw_token})
    assert resp.status_code == 200

    from app.core.database import get_sessionmaker  # noqa: PLC0415
    from app.core.identity import _delete_user_artifacts_for_tests  # noqa: PLC0415

    async with get_sessionmaker()() as cleanup:
        assert await session_lifecycle.lookup(cleanup, s1.raw_token) is None
        assert await session_lifecycle.lookup(cleanup, s2.raw_token) is None
        assert await session_lifecycle.lookup(cleanup, s3.raw_token) is None
        await _delete_user_artifacts_for_tests(cleanup, user_id=user.id)
        await cleanup.commit()


@pytest.mark.asyncio
async def test_me_exposes_broken_integrations_for_admins(db_session) -> None:
    """Admins (and Owners) see the org's broken MCP integrations; Members don't."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.domain.integrations import create_credential  # noqa: PLC0415

    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="brokens-org")
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.id, role=Role.BUILDER, handle="mem"
    )
    await create_credential(
        db_session,
        org_id=org.id,
        provider="linear",
        encrypted_access_token="enc",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["read"],
        allowed_tools=[],
        enabled=True,
        upstream_identity="bot",
        last_refresh_status="failed",
        last_refresh_failed_at=datetime.now(UTC),
    )
    a_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    m_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        a_resp = await c.get("/api/auth/me", cookies={"yaaos_session": a_sess.raw_token})
        m_resp = await c.get("/api/auth/me", cookies={"yaaos_session": m_sess.raw_token})

    a_membership = next(m for m in a_resp.json()["memberships"] if m["slug"] == "brokens-org")
    m_membership = next(m for m in m_resp.json()["memberships"] if m["slug"] == "brokens-org")
    assert [b["provider"] for b in a_membership["broken_integrations"]] == ["linear"]
    assert m_membership["broken_integrations"] == []
