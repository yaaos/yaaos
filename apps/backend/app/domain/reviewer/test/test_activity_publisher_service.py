"""In-memory provider activity-stream wiring: `_activity_publisher_for`
shipping `ActivityEvent`s from a coding-agent invocation to
`core/sse_pubsub` on `channel_for(workflow_execution_id)`.

Closes the Phase 8b ledger item: "In-memory provider: taskiq worker
publishes directly to `core/sse_pubsub` (no WebSocket wire)." Workspace
command bodies now pass a publisher to `coding_agent.review`/etc., and
the in-memory pubsub fans events out to live subscribers without
needing the remote agent's WebSocket transport.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.sse_pubsub import (
    channel_for,
    subscribe,
)
from app.core.sse_pubsub.service import _reset_for_tests
from app.core.workflow import CommandContext
from app.domain.coding_agent.types import ActivityEvent
from app.domain.reviewer.commands import _activity_publisher_for

pytestmark = pytest.mark.usefixtures("redis_or_skip")


async def test_activity_publisher_fans_out_to_subscribed_channel() -> None:
    """Subscribe to the workflow's activity channel; trigger the publisher;
    expect the event to land verbatim."""
    _reset_for_tests()
    wfx_id = str(uuid4())
    ctx = CommandContext(
        workflow_execution_id=wfx_id,
        ticket_id=str(uuid4()),
        step_id="review",
        attempt=0,
    )
    publisher = _activity_publisher_for(ctx)

    received: list[dict] = []

    async def _reader() -> None:
        async for event in subscribe(channel_for(wfx_id)):
            received.append(event)
            if len(received) >= 1:
                return

    reader_task = asyncio.create_task(_reader())
    # Small sleep so the subscriber registers before we publish; the
    # InMemoryPubsub uses an asyncio.Queue per subscriber + drops on
    # no-subscribers.
    await asyncio.sleep(0.01)

    event = ActivityEvent(
        ts=datetime.now(UTC),
        kind="tool_call_started",
        message="Read: src/x.py",
        detail={"tool": "Read", "tool_use_id": "tool_1"},
    )
    await publisher(event)

    await asyncio.wait_for(reader_task, timeout=1.0)

    assert len(received) == 1
    assert received[0]["kind"] == "tool_call_started"
    assert received[0]["message"] == "Read: src/x.py"
    assert received[0]["detail"]["tool"] == "Read"


async def test_activity_publisher_no_subscribers_is_silent() -> None:
    """Publishing to a channel with no subscribers is a no-op — the
    coding-agent invocation must not block waiting for an SSE reader."""
    _reset_for_tests()
    ctx = CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="review",
        attempt=0,
    )
    publisher = _activity_publisher_for(ctx)

    event = ActivityEvent(
        ts=datetime.now(UTC),
        kind="session_start",
        message="Session started",
        detail={},
    )
    # Returns cleanly even with no subscribers.
    await publisher(event)
