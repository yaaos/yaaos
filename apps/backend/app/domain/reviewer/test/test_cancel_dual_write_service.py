"""`POST /api/reviewer/cancel` dual-writes to `cancel_pending` +
`workflow.request_cancel` for any non-terminal workflow_executions
on the ticket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

import app.web  # noqa: F401  — registers the reviewer router
from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.workflow import WorkflowExecutionRow, WorkflowState
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo
from app.domain.tickets import TicketRow


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"reviewer"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


# Stable test-fixture org id. The /api/reviewer routers are org-scoped;
# production code doesn't reference this constant.
_DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
_ORG_SLUG = "dual-write-test"


async def _seed_ticket(db_session) -> tuple[TicketRow, object]:  # type: ignore[no-untyped-def]
    """Insert a ticket + a Builder session so the cancel endpoint can
    authenticate. Returns (ticket, session)."""
    existing = await orgs_repo.get_org_by_slug(db_session, _ORG_SLUG)
    if existing is None:
        org = await orgs_repo.insert_org(db_session, slug=_ORG_SLUG)
        org.id = type(org.id)(_DEFAULT_ORG_ID)  # rebind to the id
        await db_session.flush()
        existing = org
    user = await identity_repo.insert_user(db_session, display_name="Builder")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=existing.id, role=Role.BUILDER, handle="b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)

    ticket = TicketRow(
        id=uuid4(),
        org_id=type(uuid4())(_DEFAULT_ORG_ID),
        source="github_pr",
        source_external_id=f"pr-{uuid4()}",
        title="cancel-test",
        plugin_id="github",
        repo_external_id="me/repo",
    )
    db_session.add(ticket)
    await db_session.flush()
    return ticket, sess


def _auth(sess) -> dict[str, dict[str, str] | dict[str, str]]:  # type: ignore[no-untyped-def]
    return {
        "cookies": {"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
        "headers": {"X-Org-Slug": _ORG_SLUG, "X-CSRF-Token": sess.csrf_token},
    }


@pytest.mark.asyncio
async def test_cancel_endpoint_sets_cancel_requested_on_workflow_executions(  # type: ignore[no-untyped-def]
    db_session,
):
    """A running workflow_executions row for the ticket gets
    `cancel_requested=true` after POST /api/reviewer/cancel."""
    ticket, sess = await _seed_ticket(db_session)

    wfx_running = WorkflowExecutionRow(
        ticket_id=ticket.id,
        workflow_name="pr_review_v1",
        workflow_version=1,
        state=WorkflowState.RUNNING.value,
        step_state={},
        cancel_requested=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    wfx_done = WorkflowExecutionRow(
        ticket_id=ticket.id,
        workflow_name="pr_review_v1",
        workflow_version=1,
        state=WorkflowState.DONE.value,
        step_state={},
        cancel_requested=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add_all([wfx_running, wfx_done])
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(f"/api/reviewer/cancel?ticket_id={ticket.id}", **_auth(sess))
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled_count"] >= 1

    refreshed_running = await db_session.get(WorkflowExecutionRow, wfx_running.id)
    refreshed_done = await db_session.get(WorkflowExecutionRow, wfx_done.id)
    assert refreshed_running.cancel_requested is True, "running workflow should be cancelled"
    assert refreshed_done.cancel_requested is False, "DONE workflow must not be re-cancelled"


@pytest.mark.asyncio
async def test_cancel_endpoint_no_workflows_returns_zero(db_session) -> None:  # type: ignore[no-untyped-def]
    """No workflows + no review_jobs rows → cancelled_count == 0."""
    ticket, sess = await _seed_ticket(db_session)
    await db_session.commit()

    async with _client() as c:
        resp = await c.post(f"/api/reviewer/cancel?ticket_id={ticket.id}", **_auth(sess))
    assert resp.status_code == 200
    assert resp.json()["cancelled_count"] == 0


@pytest.mark.asyncio
async def test_cancel_endpoint_404_on_missing_ticket(db_session) -> None:  # type: ignore[no-untyped-def]
    # Seed an org + session so we get past auth and into the handler.
    _, sess = await _seed_ticket(db_session)
    await db_session.commit()
    async with _client() as c:
        resp = await c.post(f"/api/reviewer/cancel?ticket_id={uuid4()}", **_auth(sess))
    assert resp.status_code == 404
