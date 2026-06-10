"""Orphan-sweep safeguard: `running` tickets with no active workflow execution → `failed`.

Verifies the audit row + status transition + grace window. Service-grade
because the sweep crosses tickets + reviewer + audit + workflow modules.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.audit_log import list_for_entity
from app.core.identity import repository as identity_repo
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer.orphan_sweep import ORPHAN_REASON, _sweep_once
from app.domain.tickets import get as get_ticket


async def _seed_running_ticket(db_session, org_id, *, ext: str, age_seconds: int) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Insert a ticket row with `created_at` shifted back by `age_seconds`."""
    tid = uuid.uuid4()
    created_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    await db_session.execute(
        text(
            "INSERT INTO tickets "
            "(id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id,"
            " created_at, updated_at)"
            " VALUES (:id, :org_id, 'github_pr', :ext, :title, 'running', 'github', 'x/y',"
            " :created_at, :created_at)"
        ),
        {
            "id": tid,
            "org_id": org_id,
            "ext": ext,
            "title": f"orphan-{ext}",
            "created_at": created_at,
        },
    )
    return tid


async def _seed_workflow_execution(  # type: ignore[no-untyped-def]
    db_session,
    ticket_id: uuid.UUID,
    *,
    state: str,
    current_step_id: str | None = None,
) -> uuid.UUID:
    """Insert a workflow_executions row for the given ticket."""
    wfx_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO workflow_executions "
            "(id, ticket_id, workflow_name, workflow_version, state, current_step_id,"
            " step_state, cancel_requested)"
            " VALUES (:id, :ticket_id, 'pr_review_v1', 1, :state, :current_step_id,"
            " '{}'::jsonb, false)"
        ),
        {
            "id": wfx_id,
            "ticket_id": ticket_id,
            "state": state,
            "current_step_id": current_step_id,
        },
    )
    return wfx_id


@pytest.mark.service
@pytest.mark.asyncio
async def test_sweep_flips_stale_running_ticket_to_failed(db_session) -> None:  # type: ignore[no-untyped-def]
    user = await identity_repo.insert_user(db_session, display_name="J")
    org = await orgs_repo.insert_org(db_session, slug="orphan-org")
    del user
    # Older than the 300 s default grace.
    stale = await _seed_running_ticket(db_session, org.org_id, ext="x/y#1", age_seconds=600)
    # Fresh row that must NOT be touched.
    fresh = await _seed_running_ticket(db_session, org.org_id, ext="x/y#2", age_seconds=10)
    await db_session.commit()

    failed = await _sweep_once()
    assert failed == 1

    stale_ticket = await get_ticket(stale, org_id=org.org_id)
    fresh_ticket = await get_ticket(fresh, org_id=org.org_id)
    assert stale_ticket.status == "failed"
    assert fresh_ticket.status == "running"

    # Audit row with the orphan reason in payload.
    audits = await list_for_entity("ticket", stale, org_id=org.org_id, kinds=["ticket.status_changed"])
    assert len(audits) == 1
    assert audits[0].payload.get("reason") == ORPHAN_REASON
    assert audits[0].payload.get("to_status") == "failed"


@pytest.mark.service
@pytest.mark.asyncio
async def test_sweep_skips_ticket_with_active_workflow_execution(db_session) -> None:  # type: ignore[no-untyped-def]
    """A `running` ticket with a non-terminal workflow_executions row must not be touched."""
    user = await identity_repo.insert_user(db_session, display_name="J")
    org = await orgs_repo.insert_org(db_session, slug="active-wfx-org")
    del user
    ticket_id = await _seed_running_ticket(db_session, org.org_id, ext="x/y#9", age_seconds=600)
    await _seed_workflow_execution(db_session, ticket_id, state="running")
    await db_session.commit()

    failed = await _sweep_once()
    assert failed == 0

    ticket = await get_ticket(ticket_id, org_id=org.org_id)
    assert ticket.status == "running"


@pytest.mark.service
@pytest.mark.asyncio
async def test_sweep_skips_ticket_stalled_at_provision_workspace(db_session) -> None:  # type: ignore[no-untyped-def]
    """Regression: a ticket stalled at ProvisionWorkspace (no reviews row yet) must not be swept.

    This is the exact production incident — workflow stuck at an early step, sweep fires,
    falsely marks the ticket failed before the agent comes online.
    """
    user = await identity_repo.insert_user(db_session, display_name="J")
    org = await orgs_repo.insert_org(db_session, slug="provision-stall-org")
    del user
    ticket_id = await _seed_running_ticket(db_session, org.org_id, ext="x/y#10", age_seconds=600)
    # Workflow is in flight but stalled at ProvisionWorkspace — no reviews row exists yet.
    await _seed_workflow_execution(
        db_session, ticket_id, state="running", current_step_id="provision_workspace"
    )
    await db_session.commit()

    failed = await _sweep_once()
    assert failed == 0

    ticket = await get_ticket(ticket_id, org_id=org.org_id)
    assert ticket.status == "running"


@pytest.mark.service
@pytest.mark.asyncio
async def test_sweep_flips_stale_running_ticket_with_only_terminal_workflow_to_failed(db_session) -> None:  # type: ignore[no-untyped-def]
    """A ticket whose only workflow execution is terminal is still an orphan.

    In production the workflow terminal hook should have already flipped the ticket,
    but the sweep is a defense-in-depth backstop for that failure mode.
    """
    user = await identity_repo.insert_user(db_session, display_name="J")
    org = await orgs_repo.insert_org(db_session, slug="terminal-wfx-org")
    del user
    ticket_id = await _seed_running_ticket(db_session, org.org_id, ext="x/y#11", age_seconds=600)
    # Only a terminal (failed) execution — the "non-terminal" guard must not fire.
    await _seed_workflow_execution(db_session, ticket_id, state="failed")
    await db_session.commit()

    failed = await _sweep_once()
    assert failed == 1

    ticket = await get_ticket(ticket_id, org_id=org.org_id)
    assert ticket.status == "failed"
