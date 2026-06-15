"""Start hook that flips the owning ticket from pending to running when
`pr_review_v1` bootstraps.

Registered in both web and worker processes via `register_reviewer_start_hooks()`.
The hook runs inside the workflow engine's bootstrap-commit transaction —
it is atomic with the workflow state write to RUNNING. A guard miss (wrong
workflow, ticket already past pending, ticket not found) is a silent no-op;
the hook never raises.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow import register_start_hook

log = structlog.get_logger("domain.reviewer.start_hook")

_HANDLED_WORKFLOW = "pr_review_v1"


async def _on_workflow_start(
    *,
    workflow_execution_id: UUID,
    workflow_name: str,
    ticket_id: UUID,
    org_id: UUID,
    session: AsyncSession,
) -> None:
    """Flip the owning ticket pending→running atomically with the workflow's
    bootstrap RUNNING write.

    No-op (no raise) when:
    - the workflow is not `pr_review_v1`
    - the ticket is not found, is owned by a different execution, or is not
      currently in `pending` (re-bootstrap from a recovery path, etc.)
    """
    if workflow_name != _HANDLED_WORKFLOW:
        return

    from app.domain import tickets  # noqa: PLC0415

    flipped = await tickets.transition_on_workflow_start(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=workflow_execution_id,
        session=session,
    )
    if not flipped:
        log.debug(
            "reviewer.start_hook.no_flip",
            ticket_id=str(ticket_id),
            workflow_execution_id=str(workflow_execution_id),
        )


def register_reviewer_start_hooks() -> None:
    """Register the reviewer start hook into the workflow engine's hook registry.

    Called explicitly from web.py / worker.py after domain/reviewer is imported —
    not at import time, so the process controls when registration happens.
    """
    register_start_hook(_on_workflow_start)
