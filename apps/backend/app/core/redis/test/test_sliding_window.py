"""core/redis sliding-window counter against real Redis."""

from __future__ import annotations

import uuid

import pytest

from app.core.redis import sliding_window_hit
from app.core.redis.service import _get_client


@pytest.fixture(autouse=True)
async def _isolate():
    yield
    from app.core.redis.service import shutdown  # noqa: PLC0415

    await shutdown()


def _unique_key() -> str:
    return f"core-redis-sw-test:{uuid.uuid4()}"


@pytest.mark.asyncio
async def test_records_until_limit_then_rejects(redis_or_skip) -> None:
    key = _unique_key()
    # First `limit` hits are accepted.
    assert await sliding_window_hit(key, limit=3, window_seconds=60) is True
    assert await sliding_window_hit(key, limit=3, window_seconds=60) is True
    assert await sliding_window_hit(key, limit=3, window_seconds=60) is True
    # The 4th is over the limit.
    assert await sliding_window_hit(key, limit=3, window_seconds=60) is False


@pytest.mark.asyncio
async def test_rejected_hit_is_not_recorded(redis_or_skip) -> None:
    key = _unique_key()
    await sliding_window_hit(key, limit=1, window_seconds=60)  # accepted
    await sliding_window_hit(key, limit=1, window_seconds=60)  # rejected, not recorded
    await sliding_window_hit(key, limit=1, window_seconds=60)  # rejected, not recorded
    # Only the single accepted hit sits in the ZSET.
    assert await _get_client().zcard(key) == 1


@pytest.mark.asyncio
async def test_hit_refreshes_ttl(redis_or_skip) -> None:
    key = _unique_key()
    await sliding_window_hit(key, limit=5, window_seconds=60)
    ttl = await _get_client().ttl(key)
    assert 0 < ttl <= 60
