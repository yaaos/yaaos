"""Single-flight claim + recovery-policy registry — .

The workspace state machine has one in-flight AgentCommand at a time.
`try_claim()` atomically assigns `current_command_id` + `current_holder_workflow_id`
to a workspace ONLY if no other command holds it; it's the engine's gate
into the wire protocol. `release_claim()` clears the claim after the
terminal event has been observed (failure-report-precedes-disposal).

A separate recovery-policy registry maps AgentCommand failure labels
(e.g. `auth_expired`) to lifecycle WorkflowCommand kinds (e.g.
`RefreshWorkspaceAuth`). When the engine sees a recoverable failure event
on an AgentCommand, it consults this registry to pick the WorkflowCommand
that runs before re-dispatching the original.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

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


# ── Recovery policy registry ────────────────────────────────────────────


_RECOVERY_POLICIES: dict[str, str] = {}


def register_recovery_policy(*, failure_label: str, command_kind: str) -> None:
    """Map an AgentCommand failure label (e.g. `auth_expired`) to a
    WorkflowCommand kind that the engine inserts before re-dispatching the
    original step. Idempotent for the same mapping; raises on conflict so
    typos surface at boot."""
    existing = _RECOVERY_POLICIES.get(failure_label)
    if existing is not None and existing != command_kind:
        raise ValueError(
            f"recovery policy for '{failure_label}' already maps to '{existing}', "
            f"refusing to remap to '{command_kind}'"
        )
    _RECOVERY_POLICIES[failure_label] = command_kind


def get_recovery_policy(failure_label: str) -> str | None:
    """Look up the WorkflowCommand kind that recovers `failure_label`, or
    None if no policy is registered for it (no automatic recovery → the
    engine falls through to Tier-2 retry)."""
    return _RECOVERY_POLICIES.get(failure_label)


def registered_recovery_labels() -> list[str]:
    return sorted(_RECOVERY_POLICIES.keys())


def clear_recovery_policies() -> None:
    """Clear all registered recovery policies."""
    _RECOVERY_POLICIES.clear()


# only Tier-1 policy at boot. The actual `RefreshWorkspaceAuth`
# WorkflowCommand registers in Phase 4 alongside the rest of the reviewer
# workflow; recording the mapping here keeps it close to the workspace
# state machine that consumes it.
register_recovery_policy(failure_label="auth_expired", command_kind="RefreshWorkspaceAuth")
