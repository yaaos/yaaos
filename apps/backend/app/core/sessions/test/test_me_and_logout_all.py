"""Coverage for `/api/auth/me` and `/api/auth/logout-all`."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _auth_web  # noqa: F401
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
        db_session, user_id=user.id, org_id=org_a.org_id, role=Role.OWNER, handle="jack"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_b.org_id, role=Role.ADMIN, handle="jk"
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
    from sqlalchemy import text  # noqa: PLC0415

    from app.core.database import get_sessionmaker  # noqa: PLC0415
    from app.testing.seed import delete_user_artifacts as _delete_user_artifacts_for_tests  # noqa: PLC0415

    async with get_sessionmaker()() as cleanup:
        await cleanup.execute(text("DELETE FROM memberships WHERE user_id = :uid"), {"uid": user.id})
        await cleanup.execute(
            text("DELETE FROM orgs WHERE id = ANY(:ids)"), {"ids": [org_a.org_id, org_b.org_id]}
        )
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
    from app.testing.seed import delete_user_artifacts as _delete_user_artifacts_for_tests  # noqa: PLC0415

    async with get_sessionmaker()() as cleanup:
        assert await session_lifecycle.lookup(cleanup, s1.raw_token) is None
        assert await session_lifecycle.lookup(cleanup, s2.raw_token) is None
        assert await session_lifecycle.lookup(cleanup, s3.raw_token) is None
        await _delete_user_artifacts_for_tests(cleanup, user_id=user.id)
        await cleanup.commit()


@pytest.mark.asyncio
async def test_me_memberships_have_no_broken_integrations_field(db_session) -> None:
    """/api/auth/me memberships do not expose broken_integrations — that lives
    in GET /api/integrations/broken-summary (domain/integrations)."""
    user = await identity_repo.insert_user(db_session, display_name="A")
    org = await orgs_repo.insert_org(db_session, slug="me-no-broken-org")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.ADMIN, handle="adm"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        resp = await c.get("/api/auth/me", cookies={"yaaos_session": sess.raw_token})

    assert resp.status_code == 200
    body = resp.json()
    membership = next(m for m in body["memberships"] if m["slug"] == "me-no-broken-org")
    assert "broken_integrations" not in membership
