"""Run the neutral contract with memory and Redis independently."""

from __future__ import annotations

import pytest

from app.core.cache import set_cache_for_tests, shutdown
from app.core.cache.memory import _MemoryProvider
from app.core.cache.pubsub import _RedisProvider


@pytest.fixture
def _memory_provider():
    return _MemoryProvider()


@pytest.fixture
def _redis_provider(redis_or_skip):
    return _RedisProvider()


@pytest.fixture(params=["_memory_provider", "_redis_provider"], ids=["memory", "redis"])
def cache_provider(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
async def bound_cache(cache_provider):
    with set_cache_for_tests(provider=cache_provider):
        try:
            yield cache_provider
        finally:
            await shutdown()
