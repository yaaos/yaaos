"""Service-level coverage for GET /api/tickets/dashboard ().

Asserts the `{stats, in_flight, needs_attention}` shape, status-meta
projection, and Builder-grade auth.
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


async def _seed_ticket(db_session, org_id, status: str, title: str) -> None:  # type: ignore[no-untyped-def]
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', :ext, :title, :status, 'github', 'x/y')"
        ),
        {
            "id": uuid.uuid4(),
            "org_id": org_id,
            "ext": f"x/y#{title}",
            "title": title,
            "status": status,
        },
    )


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="B")
    org = await orgs_repo.insert_org(db_session, slug="dash-org")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.BUILDER, handle="b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)

    # Seed: 2 in_review (→ running), 1 complete (→ done), 1 abandoned (→ cancelled).
    await _seed_ticket(db_session, org.id, "running", "running-1")
    await _seed_ticket(db_session, org.id, "running", "running-2")
    await _seed_ticket(db_session, org.id, "done", "done-1")
    await _seed_ticket(db_session, org.id, "cancelled", "cancelled-1")
    await db_session.commit()
    yield {"org": org, "sess": sess}


def _auth(sess, slug: str):  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Org-Slug": slug, "X-CSRF-Token": sess.csrf_token},
    }


@pytest.mark.asyncio
async def test_dashboard_unauthenticated_rejects(seeded) -> None:
    """No cookie → 401; cookie but no X-Org-Slug → 400. Either way, no body."""
    async with _client() as c:
        r_no_cookie = await c.get(
            "/api/tickets/dashboard",
            headers={"X-Org-Slug": seeded["org"].slug},
        )
        r_no_slug = await c.get(
            "/api/tickets/dashboard",
            cookies={"yaaos_session": seeded["sess"].raw_token},
        )
    assert r_no_cookie.status_code == 401
    assert r_no_slug.status_code in (400, 403)


@pytest.mark.asyncio
async def test_dashboard_returns_shape_and_projects_status(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/tickets/dashboard", **_auth(seeded["sess"], seeded["org"].slug))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"stats", "in_flight", "needs_attention"}
    assert set(body["stats"].keys()) == {
        "in_flight",
        "hitl_pending",
        "completed_today",
        "failed_today",
    }
    # Two in_review rows project to status "running".
    assert body["stats"]["in_flight"] == 2
    assert len(body["in_flight"]) == 2
    # No findings on any seeded ticket → needs_attention is empty.
    assert body["needs_attention"] == []


@pytest.mark.asyncio
async def test_dashboard_in_flight_capped_at_10(seeded, db_session) -> None:
    """If the org has many running tickets, the band caps at 10."""
    for i in range(12):
        await _seed_ticket(db_session, seeded["org"].id, "running", f"extra-{i}")
    await db_session.commit()

    async with _client() as c:
        r = await c.get("/api/tickets/dashboard", **_auth(seeded["sess"], seeded["org"].slug))
    body = r.json()
    assert body["stats"]["in_flight"] == 14
    assert len(body["in_flight"]) == 10
