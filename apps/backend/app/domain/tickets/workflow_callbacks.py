"""Workflow lifecycle callbacks for ticket status transitions.

These async functions are attached directly to workflow definitions via the
`on_start` and `on_terminal` fields on `Workflow`. They run inside the
workflow engine's commit transaction and are atomic with the engine's state
write. Neither function commits — the engine commits after the callback returns.

Both accept `**_` to absorb engine kwargs they don't use, so the same
functions work as either start or terminal callbacks without breaking when the
engine adds future kwargs.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow import WorkflowState

log = structlog.get_logger("domain.tickets.workflow_callbacks")

_STATE_TO_STATUS = {
    WorkflowState.DONE: "done",
    WorkflowState.FAILED: "failed",
    WorkflowState.CANCELLED: "cancelled",
}


async def transition_ticket_on_start(
    *,
    workflow_execution_id,  # type: ignore[no-untyped-def]
    workflow_name,
    ticket_id,
    org_id,
    session: AsyncSession,
    **_,
) -> None:
    """Flip the owning ticket pending → running atomically with the workflow
    bootstrap RUNNING write.

    Guard misses (ticket not found, ownership mismatch, not in `pending`)
    are silent no-ops — this callback never raises.
    """
    from app.domain.tickets import transition_on_workflow_start  # noqa: PLC0415

    flipped = await transition_on_workflow_start(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=workflow_execution_id,
        session=session,
    )
    if not flipped:
        log.debug(
            "tickets.workflow_callback.start_no_flip",
            ticket_id=str(ticket_id),
            workflow_execution_id=str(workflow_execution_id),
            workflow_name=workflow_name,
        )


async def transition_ticket_on_terminal(
    *,
    workflow_execution_id,  # type: ignore[no-untyped-def]
    workflow_name,
    ticket_id,
    org_id,
    terminal_state: WorkflowState,
    failure_reason,
    session: AsyncSession,
    **_,
) -> None:
    """Flip the owning ticket to the status corresponding to the terminal
    workflow state, atomically with the engine's terminal commit.

    Guard misses (ticket not found, ownership mismatch, already terminal,
    unknown terminal_state) are silent no-ops — this callback never raises.
    """
    to_status = _STATE_TO_STATUS.get(terminal_state)
    if to_status is None:
        return

    reason = failure_reason if terminal_state is WorkflowState.FAILED else None

    from app.domain.tickets import transition_on_workflow_terminal  # noqa: PLC0415

    flipped = await transition_on_workflow_terminal(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=workflow_execution_id,
        to_status=to_status,
        reason=reason,
        session=session,
    )
    if not flipped:
        log.debug(
            "tickets.workflow_callback.terminal_no_flip",
            ticket_id=str(ticket_id),
            workflow_execution_id=str(workflow_execution_id),
            workflow_name=workflow_name,
            terminal_state=terminal_state,
        )
