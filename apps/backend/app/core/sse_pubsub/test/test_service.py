"""Redis-backed pub/sub round-trip: publish reaches subscribers, subscriber
bookkeeping balances on iterator exit, singleton identity.

Requires a live Redis at `settings.redis_url`. Tests skip cleanly when
Redis is unreachable — local dev workflow without a Redis container
shouldn't be blocked.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.sse_pubsub import (
    RedisPubsub,
    channel_for,
    get_pubsub,
    publish,
    subscribe,
    subscriber_count,
)
from app.core.sse_pubsub.service import _reset_for_tests


@pytest.fixture(autouse=True)
async def _isolate_singleton():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _unique_channel() -> str:
    """Per-test channel so concurrent tests don't cross-publish."""
    return channel_for(f"test-{uuid.uuid4()}")


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_returns_zero(redis_or_skip) -> None:
    ch = _unique_channel()
    n = await publish(ch, {"event": "x"})
    assert n == 0


@pytest.mark.asyncio
async def test_publish_fans_out_to_every_subscriber(redis_or_skip) -> None:
    ch = _unique_channel()
    received_a: list[dict] = []
    received_b: list[dict] = []

    async def _consume(target: list[dict]) -> None:
        async for evt in subscribe(ch):
            target.append(evt)
            if len(target) == 2:
                return

    a = asyncio.create_task(_consume(received_a))
    b = asyncio.create_task(_consume(received_b))

    # Yield so both subscribers register their Redis subscriptions before
    # we publish. Redis pub/sub is fire-and-forget — earlier publishes
    # would be lost.
    await asyncio.sleep(0.1)

    delivered_first = await publish(ch, {"i": 1})
    delivered_second = await publish(ch, {"i": 2})

    await asyncio.wait_for(asyncio.gather(a, b), timeout=2.0)

    assert delivered_first == 2
    assert delivered_second == 2
    assert received_a == [{"i": 1}, {"i": 2}]
    assert received_b == [{"i": 1}, {"i": 2}]


@pytest.mark.asyncio
async def test_subscriber_count_balances_on_iterator_exit(redis_or_skip) -> None:
    ch = _unique_channel()

    async def _consume_then_exit() -> None:
        async for _ in subscribe(ch):
            return  # exit after one event

    consumer = asyncio.create_task(_consume_then_exit())
    await asyncio.sleep(0.1)
    assert subscriber_count(ch) == 1

    await publish(ch, {"go": True})
    await asyncio.wait_for(consumer, timeout=2.0)
    # finally block runs after iterator exit.
    await asyncio.sleep(0.05)
    assert subscriber_count(ch) == 0


@pytest.mark.asyncio
async def test_get_pubsub_returns_singleton() -> None:
    # Construction is lazy and doesn't connect — safe without Redis.
    assert get_pubsub() is get_pubsub()
    assert isinstance(get_pubsub(), RedisPubsub)
