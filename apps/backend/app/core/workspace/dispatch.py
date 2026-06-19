"""Single-flight claim for workspace AgentCommands.

The workspace state machine has one in-flight AgentCommand at a time.
`try_claim()` atomically assigns `current_command_id` to a workspace ONLY if no
other command holds it; it's the engine's gate into the wire protocol.
`release_claim()` clears the claim after the terminal event has been observed
(failure-report-precedes-disposal).

`dispatch_via_workspace` is the Layer 2 dispatch helper — it looks up the
workspace row, enqueues the command, pins to the owning agent, and optionally
claims the workspace. All workspace dispatch helpers except `ProvisionWorkspace`
(which has no row yet) route through this function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import enqueue_command, pin_command_to_agent
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceClaimFailed, WorkspaceNotFoundError

if TYPE_CHECKING:
    from app.core.agent_gateway import AgentCommand
    from app.core.workflow.types import CommandContext

log = structlog.get_logger("core.workspace.dispatch")


async def try_claim(
    workspace_id: UUID,
    *,
    command_id: UUID,
    workflow_execution_id: UUID,
    agent_id: UUID | None = None,
    session: AsyncSession,
) -> bool:
    """Atomically claim `workspace_id` for `command_id`.

    Returns True iff the row had `current_command_id IS NULL` AND was
    `status='active'`. False otherwise — caller MUST treat as "busy" and
    not dispatch. The conditional UPDATE is the single-flight gate; a
    second concurrent caller racing on the same row sees rowcount=0 and
    backs off.

    `agent_id` (the owning `WorkspaceAgentRow.id`) is written as `owning_agent_id`
    onto the row in the same UPDATE when supplied — post-provision commands pass it
    to hard-tie the workspace to the pod that ran `ProvisionWorkspace`.
    Lean-created rows already carry `owning_agent_id` from the first workspace
    event; legacy in-process rows omit it, leaving `WorkspaceRow.owning_agent_id` NULL.

    `workflow_execution_id` is accepted for API compatibility but no longer written
    to the workspace row — correlation lives exclusively on
    `agent_commands.workflow_execution_id`.

    Caller commits; the outbox row enqueueing the AgentCommand should go
    in the same transaction so claim + dispatch land atomically.
    """
    values: dict[str, UUID] = {
        "current_command_id": command_id,
    }
    if agent_id is not None:
        values["owning_agent_id"] = agent_id
    result = await session.execute(
        update(WorkspaceRow)
        .where(
            WorkspaceRow.id == workspace_id,
            WorkspaceRow.current_command_id.is_(None),
            WorkspaceRow.status == "active",
        )
        .values(**values)
    )
    claimed = bool(result.rowcount)
    if not claimed:
        log.debug(
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
    same command_id is a no-op."""
    result = await session.execute(
        update(WorkspaceRow)
        .where(
            WorkspaceRow.id == workspace_id,
            WorkspaceRow.current_command_id == command_id,
        )
        .values(current_command_id=None)
    )
    return bool(result.rowcount)


async def dispatch_via_workspace(
    *,
    command: AgentCommand,
    workspace_id: UUID,
    ctx: CommandContext,
    session: AsyncSession,
    claim_workspace: bool = False,
) -> UUID:
    """Enqueue `command` durably inside the caller's transaction (Layer 2).

    Loads the workspace row to get `org_id` + `owning_agent_id`, calls
    `enqueue_command`, pins the command to the owning agent when one is set,
    and — when `claim_workspace=True` — atomically claims the workspace via
    `try_claim`.

    Raises:
        `WorkspaceNotFoundError` — workspace row absent.
        `WorkspaceClaimFailed` — `claim_workspace=True` but workspace busy
            (current_command_id IS NOT NULL) or inactive (status != 'active').
    """
    ws_row = (
        await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
    ).scalar_one_or_none()
    if ws_row is None:
        raise WorkspaceNotFoundError(f"workspace {workspace_id} not found")

    await enqueue_command(
        org_id=ws_row.org_id,
        command=command,
        session=session,
        workflow_execution_id=UUID(ctx.workflow_execution_id),
    )
    if ws_row.owning_agent_id is not None:
        await pin_command_to_agent(command.command_id, ws_row.owning_agent_id, session=session)

    if claim_workspace:
        claimed = await try_claim(
            workspace_id,
            command_id=command.command_id,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
            session=session,
        )
        if not claimed:
            raise WorkspaceClaimFailed(f"workspace {workspace_id} is busy or inactive")

    return command.command_id


def register_workspace_recovery_policies() -> None:
    """Register workspace-level recovery policies into the workflow engine's
    recovery registry. Called explicitly from web.py / worker.py after the
    workspace module is loaded — not at import time, so the process controls
    when registration happens."""
    from app.core.workflow import register_recovery_policy  # noqa: PLC0415

    register_recovery_policy(failure_label="auth_expired", command_kind="RefreshWorkspaceAuth")
