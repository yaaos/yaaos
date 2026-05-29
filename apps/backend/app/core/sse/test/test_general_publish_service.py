"""General-event pipeline: GeneralEventKind enum, publish_general,
publish_general_after_commit, subscribe_general.

Exercises:
- After-commit semantics: commit emits, rollback does not.
- Cross-org channel isolation.

Requires real Postgres (db_session) + real Redis (redis_or_skip).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.redis import reset_pubsub
from app.core.sse import (
    GeneralEventKind,
    publish_general,
    publish_general_after_commit,
    subscribe_general,
)


@pytest.fixture(autouse=True)
async def _isolate_singleton():
    reset_pubsub()
    yield
    reset_pubsub()


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_general_after_commit_on_rollback_emits_nothing(db_session, redis_or_skip) -> None:
    """publish_general_after_commit stashes event; rollback discards it."""
    org_id = uuid.uuid4()
    received: list[dict] = []

    async def _consume() -> None:
        async for event in subscribe_general(org_id):
            received.append(event)
            return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)  # let Redis subscription register

    publish_general_after_commit(
        db_session,
        org_id=org_id,
        kind=GeneralEventKind.TICKET_STATUS_CHANGED,
        payload={"ticket_id": str(uuid.uuid4())},
    )
    await db_session.rollback()

    # Give event loop time to fire any spurious events.
    await asyncio.sleep(0.2)

    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert received == [], "rollback must not emit SSE events"


@pytest.mark.service
@pytest.mark.asyncio
async def test_publish_general_after_commit_on_commit_emits(db_session, redis_or_skip) -> None:
    """publish_general_after_commit stashes event; commit delivers it."""
    org_id = uuid.uuid4()
    ticket_id = str(uuid.uuid4())
    received: list[dict] = []

    async def _consume() -> None:
        async for event in subscribe_general(org_id):
            received.append(event)
            return

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)  # let Redis subscription register

    publish_general_after_commit(
        db_session,
        org_id=org_id,
        kind=GeneralEventKind.TICKET_STATUS_CHANGED,
        payload={"ticket_id": ticket_id},
    )
    await db_session.commit()

    await asyncio.wait_for(consumer, timeout=3.0)

    assert len(received) == 1
    evt = received[0]
    assert evt["kind"] == "ticket_status_changed"
    assert evt["ticket_id"] == ticket_id
    assert "ts" in evt


@pytest.mark.service
@pytest.mark.asyncio
async def test_cross_org_isolation(redis_or_skip) -> None:
    """Publishing on org_B's channel must not reach org_A's subscriber."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    received_by_a: list[dict] = []

    async def _consume_a() -> None:
        async for event in subscribe_general(org_a):
            received_by_a.append(event)
            return

    consumer_a = asyncio.create_task(_consume_a())
    await asyncio.sleep(0.1)  # let Redis subscription register

    await publish_general(
        org_id=org_b,
        kind=GeneralEventKind.REVIEW_STARTED,
        payload={"review_job_id": str(uuid.uuid4())},
    )

    # Give the event loop time to propagate any cross-org leak.
    await asyncio.sleep(0.2)

    consumer_a.cancel()
    try:
        await consumer_a
    except asyncio.CancelledError:
        pass

    assert received_by_a == [], "org_B publish must not reach org_A subscriber"
