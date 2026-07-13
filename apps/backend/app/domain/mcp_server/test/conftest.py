"""Test isolation for `domain/mcp_server` — flush `/register` rate-limit windows.

The per-IP burst / sustained counters behind `POST /api/mcp-server/register`
live in Redis, which (unlike Postgres) is not transaction-rolled-back between
tests. Without a per-test flush, counters accumulate across tests that share a
source IP and trip `BURST_LIMIT`, making registration tests order-dependent.
Clearing the windows before each test makes them deterministic.
"""

from __future__ import annotations

import pytest

from app.domain.mcp_server.rate_limit import delete_rate_limits


@pytest.fixture(autouse=True)
async def _flush_register_rate_limits(_redis_reachable: bool):  # type: ignore[no-untyped-def]
    if _redis_reachable:
        await delete_rate_limits()
    yield
