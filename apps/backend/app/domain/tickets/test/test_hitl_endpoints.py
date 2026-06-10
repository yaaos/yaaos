"""Service-level coverage for the HITL endpoints on /api/tickets.

- GET /api/tickets/{id}/hitl/history — past exchanges for the ticket.
- POST /api/tickets/{id}/hitl/respond — resolve the open decision.

The workflow engine's own HITL pause/resume contract is covered in
`apps/backend/app/core/workflow/test/test_state_machine.py`; these tests
exercise the HTTP layer + the workflow_execution → ticket join.
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


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="B")
    org = await orgs_repo.insert_org(db_session, slug="hitl-org")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)

    # One ticket + one workflow_execution + one pending decision so
    # /hitl/history returns at least one entry.
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'x/y#hitl', 't', 'running', 'github', 'x/y')"
        ),
        {"id": ticket_id, "org_id": org.org_id},
    )
    wfx_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO workflow_executions"
            " (id, ticket_id, workflow_name, workflow_version, state, step_state, cancel_requested,"
            "  created_at, updated_at)"
            " VALUES (:id, :tid, 'pr_review_v1', 1, 'awaiting_human', '{}'::jsonb, false,"
            "  NOW(), NOW())"
        ),
        {"id": wfx_id, "tid": ticket_id},
    )
    decision_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO pending_human_decisions"
            " (id, workflow_execution_id, question_payload, created_at)"
            " VALUES (:id, :wid, CAST(:q AS jsonb), NOW())"
        ),
        {"id": decision_id, "wid": wfx_id, "q": '{"prompt":"approve?"}'},
    )
    await db_session.commit()
    yield {"org": org, "sess": sess, "ticket_id": ticket_id, "wfx_id": wfx_id}


def _auth(sess, slug: str):  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Yaaos-Org-Slug": slug, "X-CSRF-Token": sess.csrf_token},
    }


@pytest.mark.asyncio
async def test_history_returns_pending_decision(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            f"/api/tickets/{seeded['ticket_id']}/hitl/history",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 1
    assert items[0]["question_payload"] == {"prompt": "approve?"}
    assert items[0]["resolved_at"] is None


@pytest.mark.asyncio
async def test_history_404_on_unknown_ticket(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            f"/api/tickets/{uuid.uuid4()}/hitl/history",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_history_returns_empty_for_ticket_with_no_hitl(seeded, db_session) -> None:
    """A ticket with no workflow_executions returns []."""
    bare_ticket = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status,"
            " plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'x/y#bare', 't', 'running', 'github', 'x/y')"
        ),
        {"id": bare_ticket, "org_id": seeded["org"].org_id},
    )
    await db_session.commit()
    async with _client() as c:
        r = await c.get(
            f"/api/tickets/{bare_ticket}/hitl/history",
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.service
@pytest.mark.asyncio
async def test_respond_resolves_pending_decision_and_transitions_workflow(seeded, db_session) -> None:
    """POST /hitl/respond stamps `pending_human_decisions.resolved_at` and
    flips the workflow state out of `awaiting_human`."""
    async with _client() as c:
        r = await c.post(
            f"/api/tickets/{seeded['ticket_id']}/hitl/respond",
            json={"answer": "yes"},
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 200, r.text

    # The pending decision row now has resolved_at set.
    decision_row = (
        await db_session.execute(
            text(
                "SELECT resolved_at, resolution_payload FROM pending_human_decisions"
                " WHERE workflow_execution_id = :wid"
            ),
            {"wid": seeded["wfx_id"]},
        )
    ).first()
    assert decision_row is not None
    assert decision_row[0] is not None
    assert decision_row[1] == {"answer": "yes"}

    # The workflow row is no longer awaiting_human.
    wfx_state = (
        await db_session.execute(
            text("SELECT state FROM workflow_executions WHERE id = :wid"),
            {"wid": seeded["wfx_id"]},
        )
    ).scalar_one()
    assert wfx_state != "awaiting_human"


@pytest.mark.service
@pytest.mark.asyncio
async def test_respond_409_when_decision_already_resolved(seeded, db_session) -> None:
    """The workflow is still awaiting_human but its decision row is already
    resolved (race between two responders) — endpoint returns 409."""
    await db_session.execute(
        text(
            "UPDATE pending_human_decisions SET resolved_at = NOW(),"
            " resolution_payload = CAST('{}' AS jsonb)"
            " WHERE workflow_execution_id = :wid"
        ),
        {"wid": seeded["wfx_id"]},
    )
    await db_session.commit()
    async with _client() as c:
        r = await c.post(
            f"/api/tickets/{seeded['ticket_id']}/hitl/respond",
            json={"answer": "yes"},
            **_auth(seeded["sess"], seeded["org"].slug),
        )
    assert r.status_code == 409
