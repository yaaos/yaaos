"""Approximate rate-limit behavior and Redis expiry/layout regression tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.cache import allow_hit, clear_prefix, count_members, set_cache_for_tests, shutdown
from app.core.cache.pubsub import _RedisProvider


@pytest.mark.asyncio
async def test_records_until_limit_then_rejects(bound_cache) -> None:
    key = f"cache-window:{uuid.uuid4()}"
    try:
        for _ in range(3):
            assert await allow_hit(key, limit=3, window_seconds=60) is True
        assert await allow_hit(key, limit=3, window_seconds=60) is False
        assert await allow_hit(key, limit=3, window_seconds=60) is False
        assert await count_members(key) == 3
    finally:
        await clear_prefix(key)


@pytest.mark.asyncio
async def test_expired_history_no_longer_counts(bound_cache) -> None:
    key = f"cache-window-expiry:{uuid.uuid4()}"
    try:
        assert await allow_hit(key, limit=1, window_seconds=1)
        assert not await allow_hit(key, limit=1, window_seconds=1)
        await asyncio.sleep(1.1)
        assert await allow_hit(key, limit=1, window_seconds=1)
    finally:
        await clear_prefix(key)


@pytest.mark.asyncio
async def test_only_accepted_hits_refresh_ttl(redis_or_skip) -> None:
    provider = _RedisProvider()
    key = f"cache-window-ttl:{uuid.uuid4()}"
    with set_cache_for_tests(provider=provider):
        client = provider._get_client()
        try:
            assert await allow_hit(key, limit=1, window_seconds=60)
            assert 0 < await client.ttl(key) <= 60
            await client.pexpire(key, 5000)
            assert not await allow_hit(key, limit=1, window_seconds=60)
            assert 0 < await client.pttl(key) <= 5000
            assert await client.zcard(key) == 1
            assert await allow_hit(key, limit=2, window_seconds=60)
            assert await client.pttl(key) > 5000
            assert await client.zcard(key) == 2
        finally:
            await clear_prefix(key)
            await shutdown()
