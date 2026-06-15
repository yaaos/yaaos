"""Lifecycle commands — `CleanupWorkspace`, `ProvisionWorkspace`, `RefreshWorkspaceAuth`.

Covers:
- CleanupWorkspace: missing workspace_id (idempotent success), invalid
  uuid (failure), happy-path close that flips the WorkspaceRow status
  to `expired`, unknown id (idempotent).
- ProvisionWorkspace: execute() always returns failure (the engine always
  takes the Workspace-category dispatch path, never calls execute()).
  The dispatch() path is covered by test_lean_lifecycle_service.py.
- RefreshWorkspaceAuth: execute() returns success (engine dispatches the
  AgentCommand on the remote path; inline returns success).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

from sqlalchemy import select

from app.core.workflow import CommandContext
from app.core.workspace.commands import CleanupWorkspace, ProvisionWorkspace, RefreshWorkspaceAuth
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="cleanup",
        attempt=0,
    )


async def test_cleanup_with_no_workspace_id_succeeds_silently() -> None:
    """Provision failed mid-workflow → cleanup runs with no workspace_id.
    Treat as success so the workflow drains rather than re-failing."""
    outcome = await CleanupWorkspace().execute({}, _ctx())
    assert outcome.label == "success"


async def test_cleanup_with_invalid_workspace_id_fails() -> None:
    outcome = await CleanupWorkspace().execute({"workspace_id": "not-a-uuid"}, _ctx())
    assert outcome.label == "failure"
    assert "invalid workspace_id" in (outcome.failure_reason or "")


async def test_cleanup_flips_row_to_expired(db_session) -> None:  # type: ignore[no-untyped-def]
    from app.testing.seed import seed_agent  # noqa: PLC0415

    org_id = uuid4()
    ws_id = uuid7()
    agent = await seed_agent(org_id=org_id, session=db_session)
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=org_id,
            owning_agent_id=agent["id"],
            provider_id="remote_agent",
            spec={"sha": "deadbeef"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db_session.commit()

    outcome = await CleanupWorkspace().execute({"workspace_id": str(ws_id)}, _ctx())
    assert outcome.label == "success"

    row = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws_id))).scalar_one()
    assert row.status == WorkspaceStatus.EXPIRED.value


async def test_cleanup_unknown_workspace_succeeds_silently(db_session) -> None:  # type: ignore[no-untyped-def]
    """Phantom workspace_id (never existed, or already destroyed): close_workspace
    is a no-op on rows that aren't in active/creating. CleanupWorkspace returns
    success — idempotent."""
    _ = db_session  # ensure schema exists for the close_workspace path
    outcome = await CleanupWorkspace().execute({"workspace_id": str(uuid4())}, _ctx())
    assert outcome.label == "success"


# ── ProvisionWorkspace ─────────────────────────────────────────────────


async def test_provision_execute_always_returns_failure() -> None:
    """ProvisionWorkspace.execute() always returns failure — the engine takes
    the Workspace-category dispatch path and never calls execute() in production.
    This guard surfaces mistaken direct calls immediately."""
    outcome = await ProvisionWorkspace().execute({}, _ctx())
    assert outcome.label == "failure"
    assert "not the dispatch path" in (outcome.failure_reason or "")


# ── RefreshWorkspaceAuth ────────────────────────────────────────────────


async def test_refresh_workspace_auth_execute_returns_success() -> None:
    """RefreshWorkspaceAuth.execute() returns success. On the remote path
    the engine dispatches the AgentCommand; the inline body is a stub for
    test providers."""
    outcome = await RefreshWorkspaceAuth().execute({}, _ctx())
    assert outcome.label == "success"
