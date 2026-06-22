"""End-to-end coverage of GET /api/audit."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel

import app.core.sessions  # noqa: F401  -- triggers auth route registration
from app.core.audit_log import Actor, audit
from app.core.auth import AuthMiddleware, Role
from app.core.identity import insert_user, mint_session
from app.domain.orgs import insert_membership, insert_org

# audit_web is loaded by domain.orgs.__init__ — no explicit import needed


class _Payload(BaseModel):
    note: str


def _app() -> FastAPI:
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"audit"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    owner = await insert_user(db_session, display_name="Owner")
    member = await insert_user(db_session, display_name="Member")
    org = await insert_org(db_session, slug="audit-endpoint")
    await insert_membership(db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="own")
    await insert_membership(db_session, user_id=member.id, org_id=org.org_id, role=Role.BUILDER, handle="mem")
    owner_session = await mint_session(db_session, user_id=owner.id, workspace_id=None)
    member_session = await mint_session(db_session, user_id=member.id, workspace_id=None)
    # Two distinguishable audit rows.
    await audit(
        "user",
        owner.id,
        "logged_in",
        _Payload(note="a"),
        Actor.user(user_id=owner.id),
        org_id=org.org_id,
        session=db_session,
    )
    await audit(
        "user",
        owner.id,
        "logout",
        _Payload(note="b"),
        Actor.user(user_id=owner.id),
        org_id=org.org_id,
        session=db_session,
    )
    await db_session.commit()
    yield {"org": org, "owner_session": owner_session, "member_session": member_session}


@pytest.mark.asyncio
async def test_audit_endpoint_admin_can_read(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/audit",
            cookies={"yaaos_session": seeded["owner_session"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) >= 2
    assert all(row["actor_kind"] == "user" for row in rows)


@pytest.mark.asyncio
async def test_audit_endpoint_member_role_rejected(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/audit",
            cookies={"yaaos_session": seeded["member_session"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_audit_endpoint_filters_by_action(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/audit",
            params={"action": "logout"},
            cookies={"yaaos_session": seeded["owner_session"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200
    rows = r.json()
    assert all(row["kind"] == "logout" for row in rows)
