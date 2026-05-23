"""Rate limit on identity exchange."""

from __future__ import annotations

import pytest

from app.core.agent_gateway.rate_limit import (
    PER_IP_LIMIT,
    PER_POD_LIMIT,
    RateLimitedError,
    check_identity_exchange,
)

pytestmark = pytest.mark.usefixtures("redis_or_skip")


async def test_per_ip_limit_kicks_in() -> None:
    ip = f"10.0.0.{__import__('random').randint(1, 250)}"
    for _ in range(PER_IP_LIMIT):
        await check_identity_exchange(source_ip=ip, agent_pod_id=None)
    with pytest.raises(RateLimitedError) as exc_info:
        await check_identity_exchange(source_ip=ip, agent_pod_id=None)
    assert exc_info.value.axis == "ip"


async def test_per_pod_limit_kicks_in() -> None:
    pod = f"pod-{__import__('uuid').uuid4()}"
    for _ in range(PER_POD_LIMIT):
        await check_identity_exchange(source_ip=None, agent_pod_id=pod)
    with pytest.raises(RateLimitedError) as exc_info:
        await check_identity_exchange(source_ip=None, agent_pod_id=pod)
    assert exc_info.value.axis == "pod"


async def test_none_inputs_skip_axis() -> None:
    # No-op when both axes are None.
    await check_identity_exchange(source_ip=None, agent_pod_id=None)
