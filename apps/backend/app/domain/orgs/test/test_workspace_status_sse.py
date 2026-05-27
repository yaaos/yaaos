"""Phase 8b follow-on — SSE activity-stream generator.

The HTTP endpoint at `GET /api/workspaces/workflows/{id}/activity` is a
thin wrapper around `_activity_event_stream` plus auth + ownership checks.
End-to-end HTTP-level tests against httpx-ASGITransport hang on close for
indefinite streams, so we test the generator directly — the publish →
SSE-frame translation is the actual unit, and the route handler is small
enough that the wrapper's auth gate and ownership lookup are covered by
inspection.

Demand-pull semantics are tested separately: `core/sse_pubsub.publish()`
returns 0 deliveries when no subscriber is attached, so a webhook-
triggered review with no UI tab open generates zero activity-stream
traffic — verified in `core/sse_pubsub/test/test_service.py`.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.core.sse_pubsub import channel_for, publish
from app.core.sse_pubsub.service import _reset_for_tests as _reset_pubsub
from app.domain.orgs.workspace_status_web import _activity_event_stream


@pytest.fixture(autouse=True)
def _isolate_pubsub() -> None:
    _reset_pubsub()
    yield
    _reset_pubsub()


@pytest.mark.asyncio
async def test_activity_event_stream_emits_sse_frame_for_published_event() -> None:
    """A consumer of `_activity_event_stream` receives one SSE-shaped
    frame per event published to the matching channel.

    The generator is what the FastAPI route wraps; the wrapping
    StreamingResponse just relays bytes."""
    wfx_id = uuid4()
    channel = channel_for(str(wfx_id))

    gen = _activity_event_stream(wfx_id)
    collector = asyncio.create_task(gen.__anext__())
    # Yield control so the generator registers its subscriber inside
    # core/sse_pubsub before we publish — otherwise the event would land
    # before the queue exists and would be dropped.
    await asyncio.sleep(0.05)

    delivered = await publish(channel, {"kind": "agent.thought", "text": "hi"})
    assert delivered >= 1

    frame = await asyncio.wait_for(collector, timeout=2)
    await gen.aclose()

    assert frame.startswith(b"data: ")
    assert frame.endswith(b"\n\n")
    assert b'"kind": "agent.thought"' in frame
    assert b'"text": "hi"' in frame


@pytest.mark.asyncio
async def test_activity_event_stream_demand_pull_no_subscriber_is_noop() -> None:
    """When no consumer has attached to the channel, `publish()` returns
    0 deliveries — the architecture's demand-pull invariant."""
    wfx_id = uuid4()
    channel = channel_for(str(wfx_id))
    n = await publish(channel, {"kind": "agent.thought"})
    assert n == 0
