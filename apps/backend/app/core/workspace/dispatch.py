"""Single-flight claim for workspace AgentCommands.

The workspace state machine has one in-flight AgentCommand at a time.
`try_claim()` atomically assigns `current_command_id` + `current_holder_workflow_id`
to a workspace ONLY if no other command holds it; it's the engine's gate
into the wire protocol. `release_claim()` clears the claim after the
terminal event has been observed (failure-report-precedes-disposal).
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow import register_recovery_policy
from app.core.workspace.models import WorkspaceRow

log = structlog.get_logger("core.workspace.dispatch")


async def try_claim(
    workspace_id: UUID,
    *,
    command_id: UUID,
    workflow_execution_id: UUID,
    session: AsyncSession,
) -> bool:
    """Atomically claim `workspace_id` for `command_id` + `workflow_execution_id`.

    Returns True iff the row had `current_command_id IS NULL` AND was
    `status='active'`. False otherwise — caller MUST treat as "busy" and
    not dispatch. The conditional UPDATE is the single-flight gate; a
    second concurrent caller racing on the same row sees rowcount=0 and
    backs off.

    Caller commits; the outbox row enqueueing the AgentCommand should go
    in the same transaction so claim + dispatch land atomically.
    """
    result = await session.execute(
        update(WorkspaceRow)
        .where(
            WorkspaceRow.id == workspace_id,
            WorkspaceRow.current_command_id.is_(None),
            WorkspaceRow.status == "active",
        )
        .values(
            current_command_id=command_id,
            current_holder_workflow_id=workflow_execution_id,
        )
    )
    claimed = bool(result.rowcount)
    if not claimed:
        log.info(
            "workspace.claim.busy_or_inactive",
            workspace_id=str(workspace_id),
            workflow_execution_id=str(workflow_execution_id),
        )
    return claimed


async def release_claim(
    workspace_id: UUID,
    *,
    command_id: UUID,
    session: AsyncSession,
) -> bool:
    """Release the claim if-and-only-if `command_id` still owns it. Returns
    True if the claim was released. Idempotent — second release for the
    same command_id is a no-op.

    `current_holder_workflow_id` is preserved so the workspace remembers
    which workflow last touched it; reaper / reconciliation reads it."""
    result = await session.execute(
        update(WorkspaceRow)
        .where(
            WorkspaceRow.id == workspace_id,
            WorkspaceRow.current_command_id == command_id,
        )
        .values(current_command_id=None)
    )
    return bool(result.rowcount)


# Register workspace's boot-level recovery policy into the workflow engine's
# registry. `workspace → workflow` is the kept direction (see commands.py).
register_recovery_policy(failure_label="auth_expired", command_kind="RefreshWorkspaceAuth")
