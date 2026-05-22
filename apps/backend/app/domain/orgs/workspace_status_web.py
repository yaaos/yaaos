"""HTTP wiring for the workspace-agent connection status + per-workflow
activity stream (M05 Phase 7 + Phase 8b follow-on).

| Method | Path                                            | Action               |
|--------|-------------------------------------------------|----------------------|
| GET    | `/api/workspaces/connection_status`             | `ORG_SETTINGS_READ` — aggregated heartbeat state for the current org. |
| GET    | `/api/workspaces/workflows/{id}/activity`       | `ORG_READ` — SSE stream of ActivityEvents for a workflow execution the current org owns. |

The connection-status banner polls every ~3s. The activity-stream endpoint
is the SSE consumer side of the Phase 8b WebSocket plumbing —
[`core/agent_gateway`](../core_agent_gateway.md) publishes inbound
`activity_batch` events to [`core/sse_pubsub`](../core_sse_pubsub.md)
under `activity:{workflow_execution_id}`; this handler subscribes to
that channel and writes each event back out as an SSE frame.

The architecture's demand-pull property — "no events flow when nobody's
watching" — is enforced naturally by `core/sse_pubsub`: with no
subscribers, `publish()` returns 0 deliveries (and the WebSocket
supervisor batches are gated by the `subscribe`/`unsubscribe` control
messages the registry emits, follow-on alongside agent-pod binding).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import StreamingResponse

from app.core.agent_gateway.service import connection_status_for_org
from app.core.auth.context import org_id_var
from app.core.auth.types import Action
from app.core.database import session as db_session
from app.core.sse_pubsub import channel_for
from app.core.sse_pubsub import subscribe as sse_subscribe
from app.core.webserver import RouteSpec, register_routes
from app.core.workflow.models import WorkflowExecutionRow
from app.domain.sessions.dependencies import require

router = APIRouter()


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.get("/connection_status", dependencies=[Depends(require(Action.ORG_SETTINGS_READ))])
async def get_connection_status() -> dict[str, object]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        return await connection_status_for_org(org_id, session=s)


async def _activity_event_stream(workflow_execution_id: UUID) -> AsyncIterator[bytes]:
    """Translate inbound pub/sub events into SSE frames. Each event becomes
    one `data: <json>\\n\\n` frame."""
    channel = channel_for(str(workflow_execution_id))
    async for event in sse_subscribe(channel):
        payload = json.dumps(event)
        yield f"data: {payload}\n\n".encode()


@router.get(
    "/workflows/{workflow_execution_id}/activity",
    dependencies=[Depends(require(Action.ORG_READ))],
)
async def stream_workflow_activity(
    workflow_execution_id: UUID = Path(...),
) -> StreamingResponse:
    """Subscribe an SSE client to the ActivityEvent channel for a workflow
    execution the current org owns. Returns the stream as
    `text/event-stream`; closes when the client disconnects (the
    `core/sse_pubsub` async iterator cleans up its queue on iterator exit).

    Demand-pull semantics live on the publisher side: `core/sse_pubsub`'s
    `publish()` no-ops when no subscriber is attached, so a webhook-
    triggered review with no UI tab open generates zero activity-stream
    traffic. The WS subscribe/unsubscribe control messages to the
    WorkspaceAgent land alongside the agent-pod-binding persistence in a
    future iteration; in-memory provider events flow through this
    endpoint today.
    """
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        wfx = await s.get(WorkflowExecutionRow, workflow_execution_id)
        if wfx is None:
            raise _err(404, "workflow_execution_not_found")
        # Resolve the owning org via the ticket so cross-org reads are
        # rejected even when the SPA hands the same slug + a borrowed
        # workflow id.
        from app.domain.tickets.models import TicketRow  # noqa: PLC0415

        ticket = await s.get(TicketRow, wfx.ticket_id)
        if ticket is None or ticket.org_id != org_id:
            raise _err(404, "workflow_execution_not_found")
    return StreamingResponse(
        _activity_event_stream(workflow_execution_id),
        media_type="text/event-stream",
    )


register_routes(RouteSpec(module_name="workspaces", router=router, url_prefix="/api/workspaces"))
