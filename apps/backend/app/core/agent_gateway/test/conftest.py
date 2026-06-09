"""Test isolation for `agent_gateway` — flush identity-exchange rate limits.

The per-IP / per-ARN identity-exchange counters live in Redis, which (unlike
Postgres) is not transaction-rolled-back between tests. Without a per-test
flush, counters accumulate across tests sharing a source IP and trip
`PER_IP_LIMIT`, making rate-limit and identity-exchange tests order-dependent
and flaky. Clearing the window before each test makes them deterministic.
"""

from __future__ import annotations

import pytest

from app.core.agent_gateway.rate_limit import delete_rate_limits


@pytest.fixture(autouse=True)
async def _flush_identity_rate_limits(_redis_reachable: bool):  # type: ignore[no-untyped-def]
    if _redis_reachable:
        await delete_rate_limits()
    yield
