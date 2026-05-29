"""Service test: `_activity_publisher_for` fans `ActivityEvent`s out to the
org-scoped workspace-activity channel via `publish_workspace_activity`.

`_activity_publisher_for` calls
`publish_workspace_activity(org_id=require_org_context(), ...)`. This test
drives that publisher inside an `org_context` block and asserts the event
arrives on `subscribe_workspace_activity(org_id, wfx_id)`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.redis import reset_pubsub
from app.core.sse import subscribe_workspace_activity
from app.core.workflow import CommandContext
from app.domain.coding_agent import ActivityEvent
from app.domain.reviewer.commands import _activity_publisher_for

pytestmark = pytest.mark.usefixtures("redis_or_skip")


@pytest.mark.asyncio
@pytest.mark.service
async def test_reviewer_activity_publishes_to_org_scoped_channel() -> None:
    """Run `_activity_publisher_for` inside `org_context`; assert the event
    lands on `subscribe_workspace_activity(org_id, wfx_id)`.

    The subscriber must be set up BEFORE the publish fires — small sleep
    ensures Redis SUBSCRIBE is registered before the publisher sends.
    """
    reset_pubsub()
    org_id = uuid4()
    wfx_id = uuid4()

    ctx = CommandContext(
        workflow_execution_id=str(wfx_id),
        ticket_id=str(uuid4()),
        step_id="review",
        attempt=0,
    )

    received: list[dict] = []

    async def _reader() -> None:
        async for event in subscribe_workspace_activity(org_id, wfx_id):
            received.append(event)
            if len(received) >= 1:
                return

    reader_task = asyncio.create_task(_reader())
    await asyncio.sleep(0.05)

    async with org_context(org_id, ActorKind.SYSTEM):
        publisher = _activity_publisher_for(ctx)
        event = ActivityEvent(
            ts=datetime.now(UTC),
            kind="tool_call_started",
            message="Read: src/x.py",
            detail={"tool": "Read", "tool_use_id": "tool_1"},
        )
        await publisher(event)

    await asyncio.wait_for(reader_task, timeout=2.0)

    assert len(received) == 1
    assert received[0]["kind"] == "tool_call_started"
    assert received[0]["message"] == "Read: src/x.py"
    assert received[0]["detail"]["tool"] == "Read"


@pytest.mark.asyncio
@pytest.mark.service
async def test_reviewer_activity_no_subscribers_is_silent() -> None:
    """Publishing when no subscriber is listening must not raise — best-effort."""
    reset_pubsub()
    org_id = uuid4()
    wfx_id = uuid4()

    ctx = CommandContext(
        workflow_execution_id=str(wfx_id),
        ticket_id=str(uuid4()),
        step_id="review",
        attempt=0,
    )

    async with org_context(org_id, ActorKind.SYSTEM):
        publisher = _activity_publisher_for(ctx)
        event = ActivityEvent(
            ts=datetime.now(UTC),
            kind="session_start",
            message="Session started",
            detail={},
        )
        await publisher(event)
    # Returns cleanly with no subscribers — no assertion needed beyond no raise.
