"""Terminal hook that flips the owning ticket status when `pr_review_v1` ends.

Registered in both web and worker processes via `register_reviewer_terminal_hooks()`.
The hook runs inside the workflow engine's terminal-commit transaction — it is
atomic with the workflow state write. A guard miss (wrong owner, already terminal,
ticket not found) is a silent no-op; the hook never raises.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow import WorkflowState

log = structlog.get_logger("domain.reviewer.terminal_hook")

# Only handle the one workflow registered today. Guard on name so adding
# a future workflow doesn't accidentally flip tickets it doesn't own.
_HANDLED_WORKFLOW = "pr_review_v1"

_STATE_TO_STATUS = {
    WorkflowState.DONE: "done",
    WorkflowState.FAILED: "failed",
    WorkflowState.CANCELLED: "cancelled",
}


async def _on_workflow_terminal(
    *,
    workflow_execution_id: UUID,
    workflow_name: str,
    ticket_id: UUID,
    org_id: UUID,
    terminal_state: WorkflowState,
    failure_reason: str | None,
    session: AsyncSession,
) -> None:
    """Flip the owning ticket to the terminal status corresponding to the workflow state.

    No-op (no raise) when:
    - the workflow is not `pr_review_v1`
    - the terminal state is not in the handled set
    - the ticket is not found, is owned by a different execution, or is already terminal
    """
    if workflow_name != _HANDLED_WORKFLOW:
        return

    to_status = _STATE_TO_STATUS.get(terminal_state)
    if to_status is None:
        return

    reason = failure_reason if terminal_state is WorkflowState.FAILED else None

    from app.domain import tickets  # noqa: PLC0415

    flipped = await tickets.transition_on_workflow_terminal(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=workflow_execution_id,
        to_status=to_status,
        reason=reason,
        session=session,
    )
    if not flipped:
        log.info(
            "reviewer.terminal_hook.no_flip",
            ticket_id=str(ticket_id),
            workflow_execution_id=str(workflow_execution_id),
            terminal_state=terminal_state,
        )


def register_reviewer_terminal_hooks() -> None:
    """Register the reviewer terminal hook into the workflow engine's hook registry.

    Called explicitly from web.py / worker.py after domain/reviewer is imported —
    not at import time, so the process controls when registration happens.
    Idempotent: double-registration is a no-op (identity check in the registry).
    """
    from app.core.workflow import register_terminal_hook  # noqa: PLC0415

    register_terminal_hook(_on_workflow_terminal)
