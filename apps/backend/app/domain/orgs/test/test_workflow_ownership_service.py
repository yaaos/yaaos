"""Service tests for `domain/orgs.assert_workflow_in_org`.

Drives the FastAPI dep directly — sets `org_id_var` to simulate the contextvar
that `require()` would have resolved from `X-Org-Slug`, then calls the dep
with a real UUID, verifying the two-query ownership check against real Postgres.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.auth import org_id_var
from app.core.workflow import WorkflowExecutionRow
from app.domain.orgs import assert_workflow_in_org
from app.domain.orgs import repository as orgs_repo
from app.domain.tickets import TicketRow

pytestmark = pytest.mark.service


def _make_ticket(org_id) -> TicketRow:
    return TicketRow(
        id=uuid4(),
        org_id=org_id,
        source="github_pr",
        source_external_id=f"pr-{uuid4()}",
        title="ownership-test",
        plugin_id="github",
        repo_external_id="me/repo",
    )


def _make_wfx(ticket_id) -> WorkflowExecutionRow:
    return WorkflowExecutionRow(
        ticket_id=ticket_id,
        workflow_name="pr_review_v1",
        workflow_version=1,
        state="running",
        current_step_id=None,
        pending_agent_command_id=None,
        step_state={},
        cancel_requested=False,
        otel_trace_context=None,
    )


@pytest.mark.asyncio
async def test_assert_workflow_in_org_passes_for_same_org(db_session) -> None:
    """Caller in org A, wfx in org A → no exception raised."""
    org_a = await orgs_repo.insert_org(db_session, slug="wfo-same-org-a")

    ticket = _make_ticket(org_a.id)
    db_session.add(ticket)
    await db_session.flush()

    wfx = _make_wfx(ticket.id)
    db_session.add(wfx)
    await db_session.flush()

    # Simulate org context for the caller — same org as the wfx's ticket.
    token = org_id_var.set(org_a.id)
    try:
        # Should complete without raising.
        await assert_workflow_in_org(workflow_execution_id=wfx.id)
    finally:
        org_id_var.reset(token)


@pytest.mark.asyncio
async def test_assert_workflow_in_org_404_for_cross_org(db_session) -> None:
    """Caller in org A, wfx in org B → HTTPException(404) raised."""
    org_a = await orgs_repo.insert_org(db_session, slug="wfo-cross-a")
    org_b = await orgs_repo.insert_org(db_session, slug="wfo-cross-b")

    ticket = _make_ticket(org_b.id)
    db_session.add(ticket)
    await db_session.flush()

    wfx = _make_wfx(ticket.id)
    db_session.add(wfx)
    await db_session.flush()

    # Caller is in org A, but the wfx belongs to org B.
    token = org_id_var.set(org_a.id)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await assert_workflow_in_org(workflow_execution_id=wfx.id)
    finally:
        org_id_var.reset(token)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_assert_workflow_in_org_404_for_missing_wfx(db_session) -> None:
    """Caller in org A, wfx id doesn't exist → HTTPException(404) raised."""
    org_a = await orgs_repo.insert_org(db_session, slug="wfo-missing-a")

    missing_id = uuid4()

    token = org_id_var.set(org_a.id)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await assert_workflow_in_org(workflow_execution_id=missing_id)
    finally:
        org_id_var.reset(token)

    assert exc_info.value.status_code == 404
