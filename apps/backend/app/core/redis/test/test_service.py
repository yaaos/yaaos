"""core/redis client caching + ping behavior."""

from __future__ import annotations

import asyncio

import pytest

import app.core.redis.service as redis_service
from app.core.redis.service import _get_client, _reset_clients_for_tests, ping


@pytest.fixture(autouse=True)
async def _isolate_cache():
    _reset_clients_for_tests()
    yield
    await redis_service.shutdown()


@pytest.mark.asyncio
async def test_get_client_returns_same_client_within_loop() -> None:
    a = _get_client()
    b = _get_client()
    assert a is b


@pytest.mark.asyncio
async def test_ping_returns_true_when_reachable(redis_or_skip) -> None:
    assert await ping() is True


@pytest.mark.asyncio
async def test_shutdown_clears_cache(redis_or_skip) -> None:
    await ping()  # warm the cache
    assert redis_service._clients  # populated
    await redis_service.shutdown()
    assert not redis_service._clients


def test_different_loops_get_different_clients() -> None:
    """Two separate event loops cache two separate clients — the whole
    point of the per-loop keying."""

    async def grab_client_id() -> int:
        return id(_get_client())

    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        client_a_id = loop_a.run_until_complete(grab_client_id())
        client_b_id = loop_b.run_until_complete(grab_client_id())
    finally:
        loop_a.close()
        loop_b.close()
    assert client_a_id != client_b_id
