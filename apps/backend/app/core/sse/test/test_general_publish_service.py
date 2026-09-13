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
from contextlib import aclosing

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import set_cache_for_tests, subscribe
from app.core.sse import (
    GeneralEventKind,
    publish_general,
    publish_general_after_commit,
    subscribe_general,
)


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


@pytest.mark.service
@pytest.mark.asyncio
async def test_rollback_then_reuse_session_does_not_publish_discarded_event(
    db_session: AsyncSession, redis_or_skip: None
) -> None:
    """A later commit on the same session cannot flush a rolled-back event."""
    org_id = uuid.uuid4()
    subscribed = asyncio.Event()
    received: list[dict] = []

    async def consume() -> None:
        async with aclosing(subscribe(f"{org_id}:general", on_subscribed=subscribed)) as events:
            async for event in events:
                received.append(event)
                if event.get("ticket_id") == "committed":
                    return

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=2.0)
        # Begin an actual transaction, then reuse the SAME session after rollback.
        await db_session.connection()
        publish_general_after_commit(
            db_session,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={"ticket_id": "rolled-back"},
        )
        await db_session.rollback()
        await db_session.connection()
        publish_general_after_commit(
            db_session,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={"ticket_id": "committed"},
        )
        await db_session.commit()
        await asyncio.wait_for(consumer, timeout=2.0)
        assert [event["ticket_id"] for event in received] == ["committed"]
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.service
@pytest.mark.asyncio
async def test_committed_general_events_keep_queue_order(db_session: AsyncSession, redis_or_skip) -> None:
    org_id = uuid.uuid4()
    ready = asyncio.Event()
    received: list[str] = []

    async def consume() -> None:
        async for event in subscribe(f"{org_id}:general", on_subscribed=ready):
            received.append(event["ticket_id"])
            if len(received) == 10:
                return

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(ready.wait(), timeout=2.0)
        await db_session.connection()
        for number in range(10):
            publish_general_after_commit(
                db_session,
                org_id=org_id,
                kind=GeneralEventKind.TICKET_STATUS_CHANGED,
                payload={"ticket_id": str(number)},
            )
        await db_session.commit()
        await asyncio.wait_for(consumer, timeout=3.0)
        assert received == [str(number) for number in range(10)]
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.service
@pytest.mark.asyncio
async def test_savepoint_rollback_preserves_outer_transaction_events(db_session: AsyncSession) -> None:
    org_id = uuid.uuid4()
    with set_cache_for_tests(scenario="recording") as recorder:
        await db_session.connection()
        publish_general_after_commit(
            db_session,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={"ticket_id": "outer-before"},
        )
        nested = await db_session.begin_nested()
        publish_general_after_commit(
            db_session,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={"ticket_id": "rolled-back-savepoint"},
        )
        await nested.rollback()
        publish_general_after_commit(
            db_session,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={"ticket_id": "outer-after"},
        )
        await db_session.commit()
        await asyncio.sleep(0)
        assert [event["ticket_id"] for _, event in recorder.published] == ["outer-before", "outer-after"]


@pytest.mark.service
@pytest.mark.asyncio
async def test_savepoint_commit_waits_for_outer_commit(db_session: AsyncSession) -> None:
    org_id = uuid.uuid4()
    with set_cache_for_tests(scenario="recording") as recorder:
        await db_session.connection()
        nested = await db_session.begin_nested()
        publish_general_after_commit(
            db_session,
            org_id=org_id,
            kind=GeneralEventKind.TICKET_STATUS_CHANGED,
            payload={"ticket_id": "savepoint-only"},
        )
        await nested.commit()
        await asyncio.sleep(0)
        assert recorder.published == []
        await db_session.rollback()
        await asyncio.sleep(0)
        assert recorder.published == []
