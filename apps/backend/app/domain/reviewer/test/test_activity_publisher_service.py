"""In-memory provider activity-stream wiring: `_activity_publisher_for`
shipping `ActivityEvent`s from a coding-agent invocation to
`core/sse` via `publish_workspace_activity` on the org-scoped channel.

The publisher requires an active `org_context`; events land on
`subscribe_workspace_activity(org_id, wfx_id)`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.redis import reset_pubsub
from app.core.sse import subscribe_workspace_activity
from app.core.workflow import CommandContext
from app.domain.coding_agent import ActivityEvent
from app.domain.reviewer.commands import _activity_publisher_for

pytestmark = pytest.mark.usefixtures("redis_or_skip")


async def test_activity_publisher_fans_out_to_subscribed_channel() -> None:
    """Subscribe to the workflow's org-scoped activity channel; trigger the
    publisher inside `org_context`; expect the event to land verbatim."""
    reset_pubsub()
    org_id: UUID = uuid4()
    wfx_id: UUID = uuid4()
    ctx = CommandContext(
        workflow_execution_id=str(wfx_id),
        ticket_id=str(uuid4()),
        step_id="review",
        attempt=0,
    )
    publisher = _activity_publisher_for(ctx)

    received: list[dict] = []

    async def _reader() -> None:
        async for event in subscribe_workspace_activity(org_id, wfx_id):
            received.append(event)
            if len(received) >= 1:
                return

    reader_task = asyncio.create_task(_reader())
    await asyncio.sleep(0.05)

    async with org_context(org_id, ActorKind.SYSTEM):
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
    reset_pubsub()
    org_id: UUID = uuid4()
    wfx_id: UUID = uuid4()
    ctx = CommandContext(
        workflow_execution_id=str(wfx_id),
        ticket_id=str(uuid4()),
        step_id="review",
        attempt=0,
    )
    publisher = _activity_publisher_for(ctx)

    async with org_context(org_id, ActorKind.SYSTEM):
        event = ActivityEvent(
            ts=datetime.now(UTC),
            kind="session_start",
            message="Session started",
            detail={},
        )
        # Returns cleanly even with no subscribers.
        await publisher(event)
