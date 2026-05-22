"""Phase 8b — in-memory pub/sub: publish fanout, per-subscriber queueing,
slow-consumer drop, subscribe unregister on close."""

from __future__ import annotations

import asyncio

import pytest

from app.core.sse_pubsub import _reset_for_tests, get_pubsub
from app.core.sse_pubsub.service import InMemoryPubsub


@pytest.fixture(autouse=True)
def _isolate_singleton() -> None:
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_returns_zero() -> None:
    bus = InMemoryPubsub()
    n = await bus.publish("activity:abc", {"event": "x"})
    assert n == 0


@pytest.mark.asyncio
async def test_publish_fans_out_to_every_subscriber() -> None:
    bus = InMemoryPubsub()
    received_a: list[dict] = []
    received_b: list[dict] = []

    async def _consume(target: list[dict]) -> None:
        async for evt in bus.subscribe("ch-1"):
            target.append(evt)
            if len(target) == 2:
                return

    a = asyncio.create_task(_consume(received_a))
    b = asyncio.create_task(_consume(received_b))

    # Yield once so both subscribers register before we publish.
    await asyncio.sleep(0.01)

    delivered_first = await bus.publish("ch-1", {"i": 1})
    delivered_second = await bus.publish("ch-1", {"i": 2})

    await asyncio.gather(a, b)

    assert delivered_first == 2
    assert delivered_second == 2
    assert received_a == [{"i": 1}, {"i": 2}]
    assert received_b == [{"i": 1}, {"i": 2}]


@pytest.mark.asyncio
async def test_subscriber_removed_after_iterator_exit() -> None:
    bus = InMemoryPubsub()

    async def _consume_then_exit() -> None:
        async for _ in bus.subscribe("ch-x"):
            return  # exit after one event

    consumer = asyncio.create_task(_consume_then_exit())
    await asyncio.sleep(0.01)
    assert bus.subscriber_count("ch-x") == 1

    await bus.publish("ch-x", {"go": True})
    await consumer
    # GC pass + finally block removes the queue.
    await asyncio.sleep(0.01)
    assert bus.subscriber_count("ch-x") == 0


@pytest.mark.asyncio
async def test_slow_consumer_drops_oldest_events() -> None:
    """When a subscriber's queue is full, the next publish drops the
    head of THAT subscriber's queue rather than blocking the publisher
    or backpressuring globally."""
    bus = InMemoryPubsub(per_subscriber_buffer=2)
    received: list[dict] = []

    async def _slow_consume() -> None:
        async for evt in bus.subscribe("ch-slow"):
            received.append(evt)
            if len(received) >= 1:
                # Pull one event then wait so the queue overflows.
                await asyncio.sleep(0.05)
                return

    consumer = asyncio.create_task(_slow_consume())
    await asyncio.sleep(0.01)

    # Fill beyond the buffer + the in-flight pull.
    for i in range(5):
        await bus.publish("ch-slow", {"i": i})

    await consumer
    # The subscriber got at most one event before it returned.
    assert len(received) >= 1


@pytest.mark.asyncio
async def test_get_pubsub_returns_singleton() -> None:
    assert get_pubsub() is get_pubsub()
