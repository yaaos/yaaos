"""Service test: /api/reviewer/jobs/by-ticket/{id} returns 404 (route removed)."""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

from app.core.auth import AuthMiddleware, Role
from app.core.identity import insert_user, mint_session
from app.domain.orgs import insert_membership, insert_org
from app.web import app as _web_app  # noqa: F401


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"reviewer", "tickets"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


def _auth(sess, slug: str):  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": sess.csrf_token},
    }


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await insert_user(db_session, display_name="B")
    org = await insert_org(db_session, slug="jobs-retired-org")
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="b")
    sess = await mint_session(db_session, user_id=user.id, workspace_id=None)

    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'x/y#1', 'Test ticket', 'running',"
            " 'github', 'x/y')"
        ),
        {"id": ticket_id, "org_id": org.org_id},
    )
    await db_session.commit()
    yield {"org": org, "sess": sess, "ticket_id": ticket_id}


@pytest.mark.service
@pytest.mark.asyncio
async def test_jobs_by_ticket_route_is_gone(seeded) -> None:
    """The /api/reviewer/jobs/by-ticket/{ticket_id} route no longer exists.

    Workflow-run data is served by GET /api/tickets/{id}/workflow-runs.
    """
    async with _client() as c:
        r = await c.get(
            f"/api/reviewer/jobs/by-ticket/{seeded['ticket_id']}",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 404
