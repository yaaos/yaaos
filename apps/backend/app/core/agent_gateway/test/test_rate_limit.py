"""Rate limit on identity exchange."""

from __future__ import annotations

import pytest

from app.core.agent_gateway.rate_limit import (
    PER_IP_LIMIT,
    RateLimitedError,
    check_identity_exchange,
)

pytestmark = pytest.mark.usefixtures("redis_or_skip")


async def test_per_ip_limit_kicks_in() -> None:
    # Unique key per run so the counter never collides with another test's
    # (the autouse fixture also flushes the window before each test).
    ip = f"test-{__import__('uuid').uuid4().hex}"
    for _ in range(PER_IP_LIMIT):
        await check_identity_exchange(source_ip=ip)
    with pytest.raises(RateLimitedError) as exc_info:
        await check_identity_exchange(source_ip=ip)
    assert exc_info.value.axis == "ip"


async def test_none_source_ip_skips_check() -> None:
    # No-op when source_ip is None (test harness / proxy stripping).
    await check_identity_exchange(source_ip=None)
