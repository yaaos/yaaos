"""End-to-end coverage of GET /api/audit."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel

from app.core.audit_log import Actor, audit
from app.core.auth import AuthMiddleware
from app.domain.auth import web as _auth_web  # noqa: F401
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import audit_web as _audit_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role


class _Payload(BaseModel):
    note: str


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    audit_spec = _specs["audit"]
    app.include_router(audit_spec.router, prefix=audit_spec.url_prefix or "/api/audit")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    owner = await identity_repo.insert_user(db_session, display_name="Owner")
    member = await identity_repo.insert_user(db_session, display_name="Member")
    org = await orgs_repo.insert_org(db_session, slug="audit-endpoint")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.id, role=Role.MEMBER, handle="mem"
    )
    owner_session = await session_lifecycle.create(db_session, user_id=owner.id, workspace_id=None)
    member_session = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    # Two distinguishable audit rows.
    await audit(
        "user",
        owner.id,
        "logged_in",
        _Payload(note="a"),
        Actor.user(user_id=owner.id),
        org_id=org.id,
        session=db_session,
    )
    await audit(
        "user",
        owner.id,
        "logout",
        _Payload(note="b"),
        Actor.user(user_id=owner.id),
        org_id=org.id,
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
            headers={"X-Org-Slug": seeded["org"].slug},
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
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_audit_endpoint_filters_by_action(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/audit",
            params={"action": "logout"},
            cookies={"yaaos_session": seeded["owner_session"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200
    rows = r.json()
    assert all(row["kind"] == "logout" for row in rows)
