"""Service-level coverage for GET /api/tickets ().

Asserts the `{items, next_cursor}` response shape, the new filter / sort /
search params, and the fields (`status` in 5-state vocab, `findings_count`).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

import app.web  # noqa: F401
from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.domain.orgs import repository as orgs_repo


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"tickets"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _seed_ticket(
    db_session,  # type: ignore[no-untyped-def]
    *,
    org_id,
    status: str,
    title: str,
    repo: str = "x/y",
) -> uuid.UUID:
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', :ext, :title, :status, 'github', :repo)"
        ),
        {
            "id": ticket_id,
            "org_id": org_id,
            "ext": f"{repo}#{title}",
            "title": title,
            "status": status,
            "repo": repo,
        },
    )
    return ticket_id


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="B")
    org = await orgs_repo.insert_org(db_session, slug="list-org")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await _seed_ticket(db_session, org_id=org.org_id, status="running", title="alpha", repo="x/y")
    await _seed_ticket(db_session, org_id=org.org_id, status="running", title="beta", repo="x/y")
    await _seed_ticket(db_session, org_id=org.org_id, status="done", title="gamma", repo="x/z")
    await db_session.commit()
    yield {"org": org, "sess": sess}


def _auth(sess, slug: str):  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": sess.csrf_token},
    }


@pytest.mark.asyncio
async def test_list_returns_envelope_shape(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/tickets", **_auth(seeded["sess"], seeded["org"].slug))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"items", "next_cursor"}
    assert body["next_cursor"] is None
    assert len(body["items"]) == 3


@pytest.mark.asyncio
async def test_each_row_carries_status_meta_fields(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/tickets", **_auth(seeded["sess"], seeded["org"].slug))
    item = r.json()["items"][0]
    # fields populated by Ticket.from_row + the list_tickets findings join.
    assert item["status"] in {"running", "hitl", "done", "failed", "cancelled"}
    assert item["findings_count"] == 0
    assert item["builder_kind"] in {"user", "system"}


@pytest.mark.asyncio
async def test_q_filters_title_substring(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/tickets?q=alph", **_auth(seeded["sess"], seeded["org"].slug))
    titles = [t["title"] for t in r.json()["items"]]
    assert titles == ["alpha"]


@pytest.mark.asyncio
async def test_repo_filter_narrows_results(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/tickets?repo_external_id=x/z",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    titles = [t["title"] for t in r.json()["items"]]
    assert titles == ["gamma"]
