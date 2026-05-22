"""CleanupWorkspace lifecycle command — real body.

Covers: missing workspace_id (idempotent success), invalid uuid (failure),
happy-path close that flips the WorkspaceRow status to `expired`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.core.workflow import CommandContext
from app.core.workspace.commands import CleanupWorkspace
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
    org_id = uuid4()
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=org_id,
            provider_id="in_process",
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
