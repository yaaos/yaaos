"""HTTP wiring for `core/sse`.

| Method | Path                                              | Auth                |
|--------|---------------------------------------------------|---------------------|
| GET    | `/api/sse/general`                                | `ORG_READ` — org-scoped general event stream for the caller's resolved org. |
| GET    | `/api/sse/workspace_activity/{run_id}` | `ORG_READ`. Cross-org isolation is the channel key: subscribers attach to `{caller_org}:workspace_activity:{run_id}`; publishers publish to `{owner_org}:…`, so a cross-org request silently yields an empty stream rather than 404. |

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
cancelled before the generator exits.  The event is held in a module-private
ContextVar with an eager default; tests rebind via the
`sse_shutdown_event_isolation` autouse fixture in `app/testing/isolation`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
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

# Test-override slot: set per-test by `set_shutdown_event_for_tests`.
# Production path uses `_default_shutdown_event` (lazy-created on first access).
_shutdown_event_var: ContextVar[asyncio.Event | None] = ContextVar("_shutdown_event_var", default=None)
_default_shutdown_event: asyncio.Event | None = None

# The final frame sent to the client on deploy.  `retry: 1000` asks the
# browser to reconnect in ~1 s; the comment line is ignored by `onmessage`
# so it never surfaces as a spurious event.
_SHUTDOWN_FRAME = "retry: 1000\n: server closing\n\n"


def _get_event() -> asyncio.Event:
    """Return the active shutdown event.

    Test-override (installed by `set_shutdown_event_for_tests`) takes priority.
    Production path lazy-creates a module-global event on first access so no
    composition-root binding is required.
    """
    override = _shutdown_event_var.get()
    if override is not None:
        return override
    global _default_shutdown_event
    if _default_shutdown_event is None:
        _default_shutdown_event = asyncio.Event()
    return _default_shutdown_event


@contextmanager
def set_shutdown_event_for_tests() -> Iterator[asyncio.Event]:
    """Context manager: install a fresh asyncio.Event as the SSE shutdown signal
    for the duration of the block. Test-only seam — restores the prior override
    on exit (even on exception).

    Used by `sse_shutdown_event_isolation` in `app/testing/isolation` so every
    test starts with an unset event and cannot leak a previously-set event into
    the next test.
    """
    event = asyncio.Event()
    token = _shutdown_event_var.set(event)
    try:
        yield event
    finally:
        _shutdown_event_var.reset(token)


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


async def _workspace_activity_stream(org_id: UUID, run_id: UUID) -> AsyncIterator[str]:
    """Translate workspace-activity pub/sub events into SSE frames.

    Yields a connect prelude first (see `sse_prelude`) for the same
    header-flush reason as `_general_stream`.  Races each subscription
    `__anext__` against the shutdown event for the same graceful-close reason.
    """
    yield sse_prelude()
    it = subscribe_workspace_activity(org_id, run_id).__aiter__()
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
    "/workspace_activity/{run_id}",
    dependencies=[Depends(require(Action.ORG_READ))],
)
async def stream_workspace_activity(run_id: UUID) -> StreamingResponse:
    """Subscribe an SSE client to the per-run activity event stream.

    Cross-org isolation is the channel key: subscribers attach to
    `{caller_org}:workspace_activity:{run_id}`. A request for a run owned by a
    different org subscribes to a channel nobody publishes to and yields an
    empty stream.
    """
    org_id = org_id_var.get()
    return StreamingResponse(
        _workspace_activity_stream(org_id, run_id),
        media_type="text/event-stream",
    )


register_routes(RouteSpec(module_name="sse", router=router, url_prefix="/api/sse"))
