"""Rate limit on identity exchange."""

from __future__ import annotations

import uuid
from typing import Literal

import pytest

from app.core.agent_gateway.rate_limit import (
    PER_IP_LIMIT,
    RateLimitedError,
    check_identity_exchange,
)
from app.core.cache import CacheError, set_cache_for_tests, shutdown


@pytest.mark.parametrize("scenario", ["default", "memory"])
async def test_per_ip_limit_kicks_in(scenario: Literal["default", "memory"], _redis_reachable: bool) -> None:
    if scenario == "default" and not _redis_reachable:
        pytest.skip("Redis not reachable at settings.redis_url")
    ip = f"test-{uuid.uuid4().hex}"
    with set_cache_for_tests(scenario=scenario):
        try:
            for _ in range(PER_IP_LIMIT):
                await check_identity_exchange(source_ip=ip)
            with pytest.raises(RateLimitedError) as exc_info:
                await check_identity_exchange(source_ip=ip)
            assert exc_info.value.axis == "ip"
        finally:
            await shutdown()


async def test_none_source_ip_skips_check() -> None:
    # No-op when source_ip is None (test harness / proxy stripping).
    await check_identity_exchange(source_ip=None)


async def test_cache_failure_does_not_admit_identity_exchange() -> None:
    async def unavailable(key: str, *, limit: int, window_seconds: int) -> bool:
        raise CacheError("unavailable")

    with set_cache_for_tests(scenario="memory") as provider:
        provider.allow_hit = unavailable
        with pytest.raises(CacheError):
            await check_identity_exchange(source_ip=f"test-{uuid.uuid4().hex}")
