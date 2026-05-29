"""Workspace-activity pipeline: publish_workspace_activity + subscribe_workspace_activity.

Exercises:
- Cross-org isolation: publishing on org_B's channel does not reach org_A's subscriber.
- Cross-workflow isolation: publishing on wfx_2 does not reach wfx_1's subscriber.

Requires real Redis (redis_or_skip). No Postgres needed — pipeline is stateless transport.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.redis import reset_pubsub
from app.core.sse import (
    publish_workspace_activity,
    subscribe_workspace_activity,
)


@pytest.fixture(autouse=True)
async def _isolate_singleton():
    reset_pubsub()
    yield
    reset_pubsub()


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_workspace_activity_cross_org_isolation(redis_or_skip) -> None:
    """Publishing on org_B's channel must not reach org_A's subscriber."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    wfx = uuid.uuid4()
    received_by_a: list[dict] = []

    async def _consume_a() -> None:
        async for event in subscribe_workspace_activity(org_a, wfx):
            received_by_a.append(event)
            return

    consumer_a = asyncio.create_task(_consume_a())
    await asyncio.sleep(0.1)  # let Redis subscription register

    await publish_workspace_activity(
        org_id=org_b,
        workflow_execution_id=wfx,
        payload={"type": "step_started", "step": "lint"},
    )

    # Give the event loop time to propagate any cross-org leak.
    await asyncio.sleep(0.2)

    consumer_a.cancel()
    try:
        await consumer_a
    except asyncio.CancelledError:
        pass

    assert received_by_a == [], "org_B publish must not reach org_A subscriber"


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_workspace_activity_cross_wfx_isolation(redis_or_skip) -> None:
    """Publishing on wfx_2 must not reach wfx_1's subscriber (same org)."""
    org_a = uuid.uuid4()
    wfx_1 = uuid.uuid4()
    wfx_2 = uuid.uuid4()
    received_by_wfx1: list[dict] = []

    async def _consume_wfx1() -> None:
        async for event in subscribe_workspace_activity(org_a, wfx_1):
            received_by_wfx1.append(event)
            return

    consumer_wfx1 = asyncio.create_task(_consume_wfx1())
    await asyncio.sleep(0.1)  # let Redis subscription register

    await publish_workspace_activity(
        org_id=org_a,
        workflow_execution_id=wfx_2,
        payload={"type": "step_started", "step": "lint"},
    )

    # Give the event loop time to propagate any cross-wfx leak.
    await asyncio.sleep(0.2)

    consumer_wfx1.cancel()
    try:
        await consumer_wfx1
    except asyncio.CancelledError:
        pass

    assert received_by_wfx1 == [], "wfx_2 publish must not reach wfx_1 subscriber"
