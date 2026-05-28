"""core/redis pubsub round-trip against real Redis."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.redis import aclose, publish, subscribe
from app.core.redis.service import _reset_clients_for_tests


@pytest.fixture(autouse=True)
async def _isolate():
    _reset_clients_for_tests()
    yield
    await aclose()


def _unique_channel() -> str:
    return f"core-redis-test:{uuid.uuid4()}"


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_returns_zero(redis_or_skip) -> None:
    ch = _unique_channel()
    assert await publish(ch, b"hello") == 0


@pytest.mark.asyncio
async def test_publish_fans_out_to_subscriber(redis_or_skip) -> None:
    ch = _unique_channel()
    received: list[bytes] = []

    async def _consume() -> None:
        async for payload in subscribe(ch):
            received.append(payload)
            if len(received) == 2:
                return

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)
    assert await publish(ch, b"one") == 1
    assert await publish(ch, b"two") == 1
    await asyncio.wait_for(task, timeout=2.0)
    assert received == [b"one", b"two"]
