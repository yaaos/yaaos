"""HTTP wiring for `core/sse`.

| Method | Path                                              | Auth                |
|--------|---------------------------------------------------|---------------------|
| GET    | `/api/sse/general`                                | `ORG_READ` — org-scoped general event stream for the caller's resolved org. |
| GET    | `/api/sse/workspace_activity/{workflow_execution_id}` | `ORG_READ`. Cross-org isolation is the channel key: subscribers attach to `{caller_org}:workspace_activity:{wfx_id}`; publishers publish to `{owner_org}:…`, so a cross-org request silently yields an empty stream rather than 404. |

The `/api/sse` prefix is classified as `ORG_SCOPED` in `core/auth/types.py`,
so `AuthMiddleware` enforces the `X-Yaaos-Org-Slug` header before the handler runs.
`require(Action.ORG_READ)` resolves the session → membership → sets
`org_id_var` and marks the route security resolved.

Graceful close on deploy: both stream generators race their subscription
`__anext__` against the contextvar-bound shutdown event.  When `shutdown()`
sets the event, the generator emits a final `retry:`+comment frame that tells
the browser's `EventSource` to reconnect within ~1 s, then returns — the
`StreamingResponse` completes cleanly instead of hanging on a dead socket.
The `retry:` value is 1000 ms; any already-pending `__anext__` task is
cancelled before the generator exits.  The event is bound at process startup
via `bind_shutdown_event` (composition root) and per-test via the
`sse_shutdown_event_isolation` autouse fixture in `app/testing/isolation`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextvars import ContextVar
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.core.auth import Action, org_id_var
from app.core.sessions import require
from app.core.sse.service import (
    serialize_for_sse,
    sse_prelude,
    subscribe_general,
    subscribe_workspace_activity,
)
from app.core.webserver import RouteSpec, register_routes

router = APIRouter()

_shutdown_event_var: ContextVar[asyncio.Event | None] = ContextVar("_shutdown_event_var", default=None)

# The final frame sent to the client on deploy.  `retry: 1000` asks the
# browser to reconnect in ~1 s; the comment line is ignored by `onmessage`
# so it never surfaces as a spurious event.
_SHUTDOWN_FRAME = "retry: 1000\n: server closing\n\n"


def bind_shutdown_event(event: asyncio.Event) -> None:
    """Bind `event` as the active SSE shutdown signal for the current Context.

    Called once at process startup (composition root) and once per test
    (`sse_shutdown_event_isolation` fixture in `app/testing/isolation`).
    Subsequent calls in the same Context replace the prior binding.
    """
    _shutdown_event_var.set(event)


def _get_event() -> asyncio.Event:
    """Return the active shutdown event. Raises `RuntimeError` if
    `bind_shutdown_event` has not been called — fail-fast so forgotten
    startup binds surface immediately rather than silently producing wrong state."""
    event = _shutdown_event_var.get()
    if event is None:
        raise RuntimeError(
            "sse shutdown event not bound: call bind_shutdown_event(asyncio.Event()) "
            "at process startup or use the sse_shutdown_event_isolation fixture in tests."
        )
    return event


async def shutdown() -> None:
    """Signal all active SSE stream generators to close gracefully.

    Sets the contextvar-bound shutdown event.  Each stream generator races its
    next-event await against this event; when it fires the generator emits
    a final `retry:`+comment frame and returns.  The `StreamingResponse`
    then completes cleanly so the browser's `EventSource` reconnects
    immediately instead of hanging on a dead socket.

    Registered with the web shutdown registry only — SSE is web-presence.
    """
    _get_event().set()


async def _general_stream(org_id: UUID) -> AsyncIterator[str]:
    """Translate general pub/sub events into SSE frames for the caller's org.

    Yields a connect prelude first so the client's EventSource fires `onopen`
    immediately (see `sse_prelude`); the stream would otherwise not flush
    headers until its first event.

    Races each subscription `__anext__` against the shutdown event.  When the
    event fires, emits the final close frame and returns so the
    `StreamingResponse` completes rather than hanging.
    """
    yield sse_prelude()
    it = subscribe_general(org_id).__aiter__()
    while True:
        next_task = asyncio.create_task(it.__anext__())
        shutdown_task = asyncio.create_task(_get_event().wait())
        done, pending = await asyncio.wait(
            {next_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if shutdown_task in done:
            yield _SHUTDOWN_FRAME
            return
        try:
            yield serialize_for_sse(next_task.result())
        except StopAsyncIteration:
            return


async def _workspace_activity_stream(org_id: UUID, workflow_execution_id: UUID) -> AsyncIterator[str]:
    """Translate workspace-activity pub/sub events into SSE frames.

    Yields a connect prelude first (see `sse_prelude`) for the same
    header-flush reason as `_general_stream`.  Races each subscription
    `__anext__` against the shutdown event for the same graceful-close reason.
    """
    yield sse_prelude()
    it = subscribe_workspace_activity(org_id, workflow_execution_id).__aiter__()
    while True:
        next_task = asyncio.create_task(it.__anext__())
        shutdown_task = asyncio.create_task(_get_event().wait())
        done, pending = await asyncio.wait(
            {next_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if shutdown_task in done:
            yield _SHUTDOWN_FRAME
            return
        try:
            yield serialize_for_sse(next_task.result())
        except StopAsyncIteration:
            return


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
    dependencies=[Depends(require(Action.ORG_READ))],
)
async def stream_workspace_activity(workflow_execution_id: UUID) -> StreamingResponse:
    """Subscribe an SSE client to the per-workflow activity event stream.

    Cross-org isolation is the channel key: subscribers attach to
    `{caller_org}:workspace_activity:{wfx_id}`. A request for a wfx owned by a
    different org subscribes to a channel nobody publishes to and yields an
    empty stream.
    """
    org_id = org_id_var.get()
    return StreamingResponse(
        _workspace_activity_stream(org_id, workflow_execution_id),
        media_type="text/event-stream",
    )


register_routes(RouteSpec(module_name="sse", router=router, url_prefix="/api/sse"))
