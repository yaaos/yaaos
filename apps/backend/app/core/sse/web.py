"""HTTP wiring for `core/sse`.

| Method | Path                                              | Auth                |
|--------|---------------------------------------------------|---------------------|
| GET    | `/api/sse/general`                                | `ORG_READ` — org-scoped general event stream for the caller's resolved org. |
| GET    | `/api/sse/workspace_activity/{workflow_execution_id}` | `ORG_READ` + workflow-in-org ownership check (404 on cross-org). |

The `/api/sse` prefix is classified as `ORG_SCOPED` in `core/auth/types.py`,
so `AuthMiddleware` enforces the `X-Org-Slug` header before the handler runs.
`require(Action.ORG_READ)` resolves the session → membership → sets
`org_id_var` and marks the route security resolved. The workspace_activity
route adds a second dep that delegates to a boot-time-registered ownership
check (kept out of `core/sse` to avoid importing `domain/*`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse

from app.core.auth import Action, org_id_var
from app.core.sessions import require
from app.core.sse.service import (
    serialize_for_sse,
    subscribe_general,
    subscribe_workspace_activity,
    verify_workspace_activity_ownership,
)
from app.core.webserver import RouteSpec, register_routes

router = APIRouter()


async def _verify_workspace_activity_ownership(
    workflow_execution_id: UUID = Path(...),
) -> None:
    """FastAPI `Depends` thunk that delegates to the registered check.

    Kept in `web.py` so `core/sse/service.py` stays framework-agnostic.
    """
    await verify_workspace_activity_ownership(workflow_execution_id)


async def _general_stream(org_id: UUID) -> AsyncIterator[str]:
    """Translate general pub/sub events into SSE frames for the caller's org."""
    async for event in subscribe_general(org_id):
        yield serialize_for_sse(event)


async def _workspace_activity_stream(org_id: UUID, workflow_execution_id: UUID) -> AsyncIterator[str]:
    """Translate workspace-activity pub/sub events into SSE frames."""
    async for event in subscribe_workspace_activity(org_id, workflow_execution_id):
        yield serialize_for_sse(event)


@router.get("/general", dependencies=[Depends(require(Action.ORG_READ))])
async def stream_general() -> StreamingResponse:
    """Subscribe an SSE client to the general org-scoped event stream.

    Returns `text/event-stream`; closes when the client disconnects. Each
    frame is `data: <json>\\n\\n` carrying a `GeneralEventKind`-typed payload.
    Only events published to the caller's resolved org reach this stream —
    cross-org isolation is enforced by the per-org Redis channel shape.
    """
    org_id = org_id_var.get()
    return StreamingResponse(_general_stream(org_id), media_type="text/event-stream")


@router.get(
    "/workspace_activity/{workflow_execution_id}",
    dependencies=[
        Depends(require(Action.ORG_READ)),
        Depends(_verify_workspace_activity_ownership),
    ],
)
async def stream_workspace_activity(workflow_execution_id: UUID) -> StreamingResponse:
    """Subscribe an SSE client to the per-workflow activity event stream.

    The ownership check (`_verify_workspace_activity_ownership`) 404s if the
    workflow belongs to a different org — this is enforced via the registrar
    that the control-plane bootstrap wires at startup, so `core/sse` itself
    never imports `domain/*`.
    """
    org_id = org_id_var.get()
    return StreamingResponse(
        _workspace_activity_stream(org_id, workflow_execution_id),
        media_type="text/event-stream",
    )


register_routes(RouteSpec(module_name="sse", router=router, url_prefix="/api/sse"))
