"""Service tests for SSE graceful close on web shutdown.

`core/sse.shutdown()` sets a process-wide event; active stream generators
race their next event against that event.  When shutdown fires, the generator
emits a final `retry:`+comment frame so the browser's `EventSource` reconnects
immediately (using the retry hint), then returns — the `StreamingResponse`
completes cleanly instead of hanging on a dead socket until the browser's TCP
timeout.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.sse import shutdown
from app.core.sse.web import _general_stream, _reset_shutdown_event_for_tests, _workspace_activity_stream


@pytest.mark.service
@pytest.mark.asyncio
async def test_shutdown_causes_general_stream_to_emit_final_frame_and_return() -> None:
    """With a subscribed `_general_stream` active, calling `shutdown()` causes
    the generator to emit a final frame and then stop iteration (StopAsyncIteration).

    The final frame must contain a `retry:` directive so the browser's EventSource
    reconnects promptly, and an SSE comment (`: ` prefix) so it does not fire
    `onmessage` on the client.
    """
    _reset_shutdown_event_for_tests()

    org_id = uuid.uuid4()
    gen = _general_stream(org_id)

    # Drain the connect prelude
    prelude = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
    assert prelude.startswith(":"), f"expected comment prelude; got {prelude!r}"

    # Start collecting the next frame — no events will be published,
    # so this will block until shutdown fires.
    collector = asyncio.create_task(gen.__anext__())
    # Yield to let the generator reach its await point.
    await asyncio.sleep(0.05)

    # Trigger shutdown.
    await shutdown()

    # The generator should emit the final frame promptly.
    final_frame: str = await asyncio.wait_for(collector, timeout=3.0)

    # Final frame must carry a retry: directive.
    assert "retry:" in final_frame, f"final frame must contain 'retry:'; got {final_frame!r}"
    # Final frame must be a comment (or contain a comment line) so onmessage
    # never fires on the client — it is purely a transport hint.
    assert ": " in final_frame, f"final frame must contain an SSE comment; got {final_frame!r}"

    # After the final frame the generator must be exhausted.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=3.0)


@pytest.mark.service
@pytest.mark.asyncio
async def test_shutdown_causes_workspace_activity_stream_to_emit_final_frame_and_return() -> None:
    """Same contract as the general stream: `shutdown()` on a live
    `_workspace_activity_stream` emits the final frame and raises StopAsyncIteration.
    """
    _reset_shutdown_event_for_tests()

    org_id = uuid.uuid4()
    wfx_id = uuid.uuid4()
    gen = _workspace_activity_stream(org_id, wfx_id)

    # Drain the connect prelude
    prelude = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
    assert prelude.startswith(":"), f"expected comment prelude; got {prelude!r}"

    collector = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.05)

    await shutdown()

    final_frame: str = await asyncio.wait_for(collector, timeout=3.0)
    assert "retry:" in final_frame, f"final frame must contain 'retry:'; got {final_frame!r}"
    assert ": " in final_frame, f"final frame must contain an SSE comment; got {final_frame!r}"

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=3.0)


@pytest.mark.service
@pytest.mark.asyncio
async def test_shutdown_of_idle_stream_no_active_waiters() -> None:
    """Calling shutdown() when no stream is subscribed does not raise.

    Regression guard: the shutdown event must be settable even when no
    stream generator is waiting on it.
    """
    _reset_shutdown_event_for_tests()
    await shutdown()  # must not raise


@pytest.mark.service
@pytest.mark.asyncio
async def test_reset_shutdown_event_unblocks_next_stream() -> None:
    """_reset_shutdown_event_for_tests() creates a fresh event so a subsequent
    stream generator is not immediately terminated.

    Validates the test-isolation helper used in the other tests above.
    """
    _reset_shutdown_event_for_tests()
    await shutdown()  # set the event

    # Without reset the next generator would see the event already set and
    # emit the final frame immediately.  After reset it should block.
    _reset_shutdown_event_for_tests()

    org_id = uuid.uuid4()
    gen = _general_stream(org_id)
    # Drain prelude
    prelude = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
    assert prelude.startswith(":")

    # The generator should now block (not immediately terminate).
    collector = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0.05)
    assert not collector.done(), "stream should not terminate before shutdown is called again"
    # Cancel the collector task and give the event loop a tick to process the
    # cancellation before closing the generator.
    collector.cancel()
    try:
        await collector
    except asyncio.CancelledError, Exception:
        pass
    await gen.aclose()
