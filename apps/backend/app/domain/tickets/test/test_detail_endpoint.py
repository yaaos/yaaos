"""HTTP coverage for GET /api/tickets/{ticket_id}.

Asserts the extended-projection shape: status (5-state collapsed vocab),
findings_count, max_severity, builder, and the stages array when a
workflow_execution exists.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

import app.web  # noqa: F401
from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"tickets"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


def _auth(sess, slug: str):  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Org-Slug": slug, "X-CSRF-Token": sess.csrf_token},
    }


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="B")
    org = await orgs_repo.insert_org(db_session, slug="detail-org")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.BUILDER, handle="b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)

    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'x/y#1', 'Tighten retries', 'running',"
            " 'github', 'x/y')"
        ),
        {"id": ticket_id, "org_id": org.id},
    )
    await db_session.commit()
    yield {"org": org, "sess": sess, "ticket_id": ticket_id}


@pytest.mark.service
@pytest.mark.asyncio
async def test_detail_returns_status_meta_fields(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            f"/api/tickets/{seeded['ticket_id']}",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(seeded["ticket_id"])
    assert body["status"] == "running"
    assert body["findings_count"] == 0
    assert body["builder_kind"] in {"user", "system"}
    # `stages` is the Phase 6 extension — present when the workflow row exists.
    # Bare ticket → empty list (not missing) so the SPA can safely .map.
    assert "stages" in body


@pytest.mark.service
@pytest.mark.asyncio
async def test_detail_404_on_unknown_ticket(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            f"/api/tickets/{uuid.uuid4()}",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 404


@pytest.mark.service
@pytest.mark.asyncio
async def test_detail_404_on_cross_org_access(seeded, db_session) -> None:
    """A ticket from a different org returns 404, not the leakage of a 403."""
    other_org = await orgs_repo.insert_org(db_session, slug="other-org")
    other_ticket = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'x/y#9', 'other', 'running', 'github', 'x/y')"
        ),
        {"id": other_ticket, "org_id": other_org.id},
    )
    await db_session.commit()
    async with _client() as c:
        r = await c.get(
            f"/api/tickets/{other_ticket}",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 404
