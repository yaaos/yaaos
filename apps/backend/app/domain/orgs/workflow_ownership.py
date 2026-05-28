"""FastAPI dependency that 404s on cross-org workflow execution access.

Kept separate from `workspace_status_web.py` so callers that only need
the ownership guard don't pull in the SSE stream wiring.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Path
from sqlalchemy import select

from app.core.auth import require_org_context
from app.core.database import session as db_session
from app.core.workflow import WorkflowExecutionRow
from app.domain.tickets import TicketRow


async def assert_workflow_in_org(
    workflow_execution_id: UUID = Path(...),
) -> None:
    """FastAPI dep — raises ``HTTPException(404)`` if the workflow execution
    doesn't exist or its ticket belongs to a different org than the caller's.

    404 on cross-org access matches yaaos's existence-non-disclosure default:
    a caller in org A learns nothing about whether a wfx id exists in org B.
    """
    caller_org_id = require_org_context()
    async with db_session() as s:
        wfx = await s.get(WorkflowExecutionRow, workflow_execution_id)
        if wfx is None:
            raise HTTPException(status_code=404)
        ticket = (
            await s.execute(select(TicketRow).where(TicketRow.id == wfx.ticket_id))
        ).scalar_one_or_none()
        if ticket is None or ticket.org_id != caller_org_id:
            raise HTTPException(status_code=404)
