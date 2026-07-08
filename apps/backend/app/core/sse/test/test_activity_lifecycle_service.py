"""Service test: workspace-activity SSE stream lifecycle hooks.

`_workspace_activity_stream` calls the registered `on_attach` hook after
yielding the prelude and the `on_detach` hook in its `finally` block. Two
cases:

- **no-route**: `on_attach` returns `None` (run has no resolvable workspace
  route) → stream serves frames normally, `on_detach` is NOT called.
- **route found**: `on_attach` returns a token → the token is echoed to
  `on_detach` when the stream closes.

Lifecycle hooks are registered via `register_activity_subscriber_lifecycle`.
This test registers spy callables instead of the real `agent_gateway` hooks
so it requires no Redis and no extra DB rows — the seam is tested in
isolation.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from app.core.sse import register_activity_subscriber_lifecycle
from app.core.sse.web import _workspace_activity_stream


async def _drive_stream_then_cancel(
    org_id: UUID,
    run_id: UUID,
) -> str:
    """Start `_workspace_activity_stream`, consume the prelude, advance the
    generator past the `on_attach` call (by requesting the next frame in a
    short-lived task), then cancel.

    Returns the prelude frame. The generator's `finally` block (`on_detach`)
    runs when the task is cancelled and the generator is garbage-collected.
    """
    gen = _workspace_activity_stream(org_id, run_id)
    it = gen.__aiter__()

    # Frame 1: the prelude (yields immediately, no I/O).
    prelude = await it.__anext__()

    # Advance past `on_attach` into the asyncio.wait() call by requesting
    # the second frame in a separate task. Cancel after a short sleep —
    # by that point `on_attach` has already been awaited.
    consumer_task = asyncio.create_task(it.__anext__())
    await asyncio.sleep(0.1)
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError, StopAsyncIteration:
        pass

    # Explicitly close the generator so the `finally` block runs now.
    await gen.aclose()

    return prelude


@pytest.mark.asyncio
@pytest.mark.service
async def test_no_route_stream_serves_frames_without_detach() -> None:
    """When `on_attach` returns `None` (no resolvable route for the run):
    - The stream still yields the prelude SSE frame.
    - `on_attach` IS called (with the right org + run).
    - `on_detach` is NOT called — the registry stays untouched.
    """
    attach_calls: list[tuple[UUID, UUID]] = []
    detach_calls: list[tuple[UUID, str]] = []

    async def _attach(org_id: UUID, run_id: UUID) -> str | None:
        attach_calls.append((org_id, run_id))
        return None  # no-route

    async def _heartbeat(run_id: UUID, token: str) -> None:
        pass

    async def _detach(run_id: UUID, token: str) -> None:
        detach_calls.append((run_id, token))

    register_activity_subscriber_lifecycle(
        on_attach=_attach,
        on_heartbeat=_heartbeat,
        on_detach=_detach,
    )

    org_id = uuid4()
    run_id = uuid4()

    prelude = await _drive_stream_then_cancel(org_id, run_id)

    # The prelude frame is the SSE connect comment (": connected\n\n").
    assert "connected" in prelude

    # on_attach was called with the right org + run.
    assert len(attach_calls) == 1
    assert attach_calls[0] == (org_id, run_id)

    # on_detach must NOT be called when on_attach returned None.
    assert detach_calls == []


@pytest.mark.asyncio
@pytest.mark.service
async def test_route_found_detach_receives_token_on_close() -> None:
    """When `on_attach` returns a non-None token:
    - `on_detach` is called with `(run_id, token)` when the stream closes.
    - This is the path used by the real lifecycle hooks to call
      `SubscriberRegistry.untrack(run_id, conn_id)`.
    """
    attach_token = "test-conn-id|test-agent-id"
    detach_calls: list[tuple[UUID, str]] = []

    async def _attach(org_id: UUID, run_id: UUID) -> str | None:
        return attach_token  # route found

    async def _heartbeat(run_id: UUID, token: str) -> None:
        pass

    async def _detach(run_id: UUID, token: str) -> None:
        detach_calls.append((run_id, token))

    register_activity_subscriber_lifecycle(
        on_attach=_attach,
        on_heartbeat=_heartbeat,
        on_detach=_detach,
    )

    org_id = uuid4()
    run_id = uuid4()

    await _drive_stream_then_cancel(org_id, run_id)

    # Generator closed → finally block ran → on_detach called with the token.
    assert len(detach_calls) == 1
    assert detach_calls[0] == (run_id, attach_token)
