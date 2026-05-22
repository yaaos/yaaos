"""ASGI-driven coverage of `/api/memberships/*`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.audit_log import Actor
from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import invite as invite_service
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import web as _orgs_web  # noqa: F401 — registers /api/memberships
from app.domain.orgs.email import get_test_inbox
from app.domain.orgs.types import Role
from app.domain.sessions import web as _auth_web  # noqa: F401 — triggers auth.dep load


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    spec = _specs["memberships"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/memberships")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session) -> AsyncIterator[dict[str, object]]:
    get_test_inbox().clear()
    owner_user = await identity_repo.insert_user(db_session, display_name="Owner")
    member_user = await identity_repo.insert_user(db_session, display_name="Member")
    org = await orgs_repo.insert_org(db_session, slug="endpoints-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner_user.id, org_id=org.id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member_user.id, org_id=org.id, role=Role.MEMBER, handle="mem"
    )

    owner_session = await session_lifecycle.create(db_session, user_id=owner_user.id, workspace_id=None)
    member_session = await session_lifecycle.create(db_session, user_id=member_user.id, workspace_id=None)
    yield {
        "org": org,
        "owner_user": owner_user,
        "member_user": member_user,
        "owner_session": owner_session,
        "member_session": member_session,
    }
    get_test_inbox().clear()


@pytest.mark.asyncio
async def test_invite_happy_path_sends_email(seeded) -> None:
    org = seeded["org"]
    owner_session = seeded["owner_session"]
    async with _client() as c:
        resp = await c.post(
            "/api/memberships/invite",
            json={"email": "new@example.com", "role": "member"},
            cookies={
                "yaaos_session": owner_session.raw_token,
                "yaaos_csrf": owner_session.csrf_token,
            },
            headers={"X-Org-Slug": org.slug, "X-CSRF-Token": owner_session.csrf_token},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "new@example.com"
    assert body["role"] == "member"
    inbox = get_test_inbox()
    assert any(m.to == "new@example.com" for m in inbox)


@pytest.mark.asyncio
async def test_invite_member_role_rejected(seeded) -> None:
    org = seeded["org"]
    member_session = seeded["member_session"]
    async with _client() as c:
        resp = await c.post(
            "/api/memberships/invite",
            json={"email": "x@example.com", "role": "member"},
            cookies={
                "yaaos_session": member_session.raw_token,
                "yaaos_csrf": member_session.csrf_token,
            },
            headers={"X-Org-Slug": org.slug, "X-CSRF-Token": member_session.csrf_token},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_accept_invitation_happy_path(seeded, db_session) -> None:
    org = seeded["org"]
    owner_user = seeded["owner_user"]
    _, raw = await invite_service(
        db_session,
        org_id=org.id,
        email="alice@example.com",
        role=Role.MEMBER,
        invited_by_user_id=owner_user.id,
        actor=Actor.user(user_id=owner_user.id),
    )
    # Acceptor needs a session cookie.
    alice = await identity_repo.insert_user(db_session)
    alice_session = await session_lifecycle.create(db_session, user_id=alice.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            "/api/memberships/accept",
            json={"token": raw},
            cookies={"yaaos_session": alice_session.raw_token},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "member"


@pytest.mark.asyncio
async def test_accept_expired_returns_410(seeded, db_session) -> None:
    org = seeded["org"]
    owner_user = seeded["owner_user"]
    _, raw = await invite_service(
        db_session,
        org_id=org.id,
        email="ex@example.com",
        role=Role.MEMBER,
        invited_by_user_id=owner_user.id,
        actor=Actor.user(user_id=owner_user.id),
    )
    from sqlalchemy import update  # noqa: PLC0415

    from app.domain.orgs.models import InvitationRow  # noqa: PLC0415

    await db_session.execute(
        update(InvitationRow)
        .where(InvitationRow.email == "ex@example.com")
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    user = await identity_repo.insert_user(db_session)
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(
            "/api/memberships/accept",
            json={"token": raw},
            cookies={"yaaos_session": s.raw_token},
        )
    assert resp.status_code == 410
    assert resp.json()["detail"]["error"] == "invitation_expired"


@pytest.mark.asyncio
async def test_accept_used_returns_410(seeded, db_session) -> None:
    org = seeded["org"]
    owner_user = seeded["owner_user"]
    _, raw = await invite_service(
        db_session,
        org_id=org.id,
        email="used@example.com",
        role=Role.MEMBER,
        invited_by_user_id=owner_user.id,
        actor=Actor.user(user_id=owner_user.id),
    )
    user = await identity_repo.insert_user(db_session)
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        first = await c.post(
            "/api/memberships/accept",
            json={"token": raw},
            cookies={"yaaos_session": s.raw_token},
        )
        assert first.status_code == 200
        second = await c.post(
            "/api/memberships/accept",
            json={"token": raw},
            cookies={"yaaos_session": s.raw_token},
        )
    assert second.status_code == 410
    assert second.json()["detail"]["error"] == "invitation_used"


@pytest.mark.asyncio
async def test_remove_member_revokes_sessions(seeded, db_session) -> None:
    org = seeded["org"]
    owner_session = seeded["owner_session"]
    member_user = seeded["member_user"]
    member_session = seeded["member_session"]
    await db_session.commit()

    async with _client() as c:
        resp = await c.delete(
            f"/api/memberships/{member_user.id}",
            cookies={
                "yaaos_session": owner_session.raw_token,
                "yaaos_csrf": owner_session.csrf_token,
            },
            headers={"X-Org-Slug": org.slug, "X-CSRF-Token": owner_session.csrf_token},
        )
    assert resp.status_code == 200
    from app.core.database import get_sessionmaker  # noqa: PLC0415

    async with get_sessionmaker()() as s:
        assert await session_lifecycle.lookup(s, member_session.raw_token) is None
        await s.execute(
            __import__("sqlalchemy")
            .delete(__import__("app.domain.orgs.models", fromlist=["MembershipRow"]).MembershipRow)
            .where(
                __import__("app.domain.orgs.models", fromlist=["MembershipRow"]).MembershipRow.org_id
                == org.id
            )
        )
        await s.execute(
            __import__("sqlalchemy")
            .delete(__import__("app.domain.orgs.models", fromlist=["OrgRow"]).OrgRow)
            .where(__import__("app.domain.orgs.models", fromlist=["OrgRow"]).OrgRow.id == org.id)
        )
        await s.commit()


@pytest.mark.asyncio
async def test_change_role_rotates_sessions(seeded, db_session) -> None:
    org = seeded["org"]
    owner_session = seeded["owner_session"]
    member_user = seeded["member_user"]
    member_session = seeded["member_session"]
    await db_session.commit()

    async with _client() as c:
        resp = await c.patch(
            f"/api/memberships/{member_user.id}",
            json={"role": "admin"},
            cookies={
                "yaaos_session": owner_session.raw_token,
                "yaaos_csrf": owner_session.csrf_token,
            },
            headers={"X-Org-Slug": org.slug, "X-CSRF-Token": owner_session.csrf_token},
        )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
    from app.core.database import get_sessionmaker  # noqa: PLC0415

    async with get_sessionmaker()() as s:
        assert await session_lifecycle.lookup(s, member_session.raw_token) is None
        # Cleanup the seeded org so other tests see a clean slate.
        from sqlalchemy import delete  # noqa: PLC0415

        from app.domain.orgs.models import MembershipRow, OrgRow  # noqa: PLC0415

        await s.execute(delete(MembershipRow).where(MembershipRow.org_id == org.id))
        await s.execute(delete(OrgRow).where(OrgRow.id == org.id))
        await s.commit()


@pytest.mark.asyncio
async def test_list_members_returns_org_roster(seeded) -> None:
    org = seeded["org"]
    owner_session = seeded["owner_session"]
    async with _client() as c:
        resp = await c.get(
            "/api/memberships",
            cookies={"yaaos_session": owner_session.raw_token},
            headers={"X-Org-Slug": org.slug},
        )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) >= 2
    roles = {r["role"] for r in rows}
    assert "owner" in roles and "member" in roles
