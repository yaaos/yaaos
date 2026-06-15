"""Unit tests for the WorkspaceAgentReportSinkImpl.

Exercises the kind→status map, stale-claim guard, and heartbeat
reconciliation directly on the sink — no agent_gateway import.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

import pytest

from app.core.agent_gateway import CleanupWorkspaceCommand, WorkspaceEventReport, enqueue_command
from app.core.workspace.agent_report import WorkspaceAgentReportSinkImpl
from app.core.workspace.models import WorkspaceRow


async def _make_workspace_row(
    db_session,
    *,
    status: str = "active",
    command_id=None,
) -> WorkspaceRow:
    from app.testing.seed import seed_agent  # noqa: PLC0415

    agent = await seed_agent(org_id=uuid4(), session=db_session)
    return WorkspaceRow(
        id=uuid7(),
        org_id=uuid4(),
        provider_id="remote_agent",
        spec={"sha": "deadbeef"},
        status=status,
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
        current_command_id=command_id,
        owning_agent_id=agent["id"],
    )


# ── apply_workspace_event: kind→status map ─────────────────────────────


@pytest.mark.asyncio
async def test_kind_ready_maps_to_active(db_session) -> None:
    """kind='ready' must flip workspace status to 'active'."""
    sink = WorkspaceAgentReportSinkImpl()
    cmd_id = uuid7()
    ws = await _make_workspace_row(db_session, status="creating", command_id=cmd_id)
    db_session.add(ws)
    await db_session.flush()

    report = WorkspaceEventReport(workspace_id=ws.id, command_id=cmd_id, kind="ready")
    outcome = await sink.apply_workspace_event(report, db_session)

    assert outcome.accepted is True
    assert outcome.resolved_status == "active"
    await db_session.refresh(ws)
    assert ws.status == "active"


@pytest.mark.asyncio
async def test_kind_destroyed_maps_to_destroyed(db_session) -> None:
    sink = WorkspaceAgentReportSinkImpl()
    ws = await _make_workspace_row(db_session, status="destroying")
    db_session.add(ws)
    await db_session.flush()

    report = WorkspaceEventReport(workspace_id=ws.id, command_id=None, kind="destroyed")
    outcome = await sink.apply_workspace_event(report, db_session)

    assert outcome.accepted is True
    assert outcome.resolved_status == "destroyed"
    await db_session.refresh(ws)
    assert ws.status == "destroyed"


@pytest.mark.asyncio
async def test_kind_failed_maps_to_destroy_failed(db_session) -> None:
    sink = WorkspaceAgentReportSinkImpl()
    ws = await _make_workspace_row(db_session, status="destroying")
    db_session.add(ws)
    await db_session.flush()

    report = WorkspaceEventReport(workspace_id=ws.id, command_id=None, kind="failed")
    outcome = await sink.apply_workspace_event(report, db_session)

    assert outcome.accepted is True
    assert outcome.resolved_status == "destroy_failed"
    await db_session.refresh(ws)
    assert ws.status == "destroy_failed"


@pytest.mark.asyncio
async def test_unmapped_kind_does_not_write_status(db_session) -> None:
    """Unmapped kinds (e.g. 'created', 'exited') are accepted but produce no
    status write."""
    sink = WorkspaceAgentReportSinkImpl()
    ws = await _make_workspace_row(db_session, status="active")
    db_session.add(ws)
    await db_session.flush()

    report = WorkspaceEventReport(workspace_id=ws.id, command_id=None, kind="created")
    outcome = await sink.apply_workspace_event(report, db_session)

    assert outcome.accepted is True
    assert outcome.resolved_status is None
    await db_session.refresh(ws)
    assert ws.status == "active"  # unchanged


# ── apply_workspace_event: stale-claim guard ───────────────────────────


@pytest.mark.asyncio
async def test_stale_command_id_rejects_event(db_session) -> None:
    """If workspace.current_command_id ≠ event.command_id, accepted=False."""
    sink = WorkspaceAgentReportSinkImpl()
    current_cmd = uuid4()
    ws = await _make_workspace_row(db_session, status="active", command_id=current_cmd)
    db_session.add(ws)
    await db_session.flush()

    report = WorkspaceEventReport(
        workspace_id=ws.id,
        command_id=uuid4(),  # mismatched
        kind="ready",
    )
    outcome = await sink.apply_workspace_event(report, db_session)

    assert outcome.accepted is False
    await db_session.refresh(ws)
    assert ws.status == "active"  # no change


@pytest.mark.asyncio
async def test_none_current_command_id_allows_event(db_session) -> None:
    """current_command_id=None (no active claim) allows status events through."""
    sink = WorkspaceAgentReportSinkImpl()
    ws = await _make_workspace_row(db_session, status="destroying", command_id=None)
    db_session.add(ws)
    await db_session.flush()

    report = WorkspaceEventReport(workspace_id=ws.id, command_id=uuid4(), kind="destroyed")
    outcome = await sink.apply_workspace_event(report, db_session)

    assert outcome.accepted is True
    await db_session.refresh(ws)
    assert ws.status == "destroyed"


@pytest.mark.asyncio
async def test_unknown_workspace_returns_not_accepted(db_session) -> None:
    sink = WorkspaceAgentReportSinkImpl()
    report = WorkspaceEventReport(workspace_id=uuid4(), command_id=None, kind="ready")
    outcome = await sink.apply_workspace_event(report, db_session)
    assert outcome.accepted is False
    assert outcome.resolved_status is None


# ── reconcile_heartbeat ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_forgets_unknown_and_destroyed(db_session) -> None:
    sink = WorkspaceAgentReportSinkImpl()
    known = await _make_workspace_row(db_session, status="active")
    destroyed = await _make_workspace_row(db_session, status="destroyed")
    db_session.add(known)
    db_session.add(destroyed)
    await db_session.flush()

    unknown_id = uuid4()
    forgotten = await sink.reconcile_heartbeat({known.id, destroyed.id, unknown_id}, db_session)
    assert forgotten == {destroyed.id, unknown_id}


@pytest.mark.asyncio
async def test_reconcile_empty_set_returns_empty(db_session) -> None:
    sink = WorkspaceAgentReportSinkImpl()
    forgotten = await sink.reconcile_heartbeat(set(), db_session)
    assert forgotten == set()


# ── resolve_claim ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_claim_returns_holder(db_session) -> None:
    """resolve_claim reads workflow_execution_id from agent_commands, not from
    the shed workspaces.current_holder_workflow_id column."""
    sink = WorkspaceAgentReportSinkImpl()
    cmd_id = uuid7()
    workspace_id = uuid7()
    wfx_id = uuid4()
    org_id = uuid4()

    # Enqueue an agent_commands row with the expected workflow_execution_id.
    cmd = CleanupWorkspaceCommand(
        command_id=cmd_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(
        org_id=org_id,
        command=cmd,
        session=db_session,
        workflow_execution_id=wfx_id,
    )
    await db_session.flush()

    result = await sink.resolve_claim(cmd_id, db_session)
    assert result == wfx_id


@pytest.mark.asyncio
async def test_resolve_claim_returns_none_for_unknown_command(db_session) -> None:
    sink = WorkspaceAgentReportSinkImpl()
    result = await sink.resolve_claim(uuid4(), db_session)
    assert result is None
