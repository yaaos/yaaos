"""Rate limiting for `/identity/exchange`.

Sliding-window counters per source IP and per `agent_pod_id`, backed by
Redis. Burst guards against credential-stuffing and STS-replay flooding.

POC limits:
- 10 attempts / minute per source IP
- 100 attempts / hour per agent_pod_id

On limit hit, raise `RateLimitedError` with the limiting axis; the
endpoint maps it to HTTP 429 with `identity_exchange.rate_limited` audit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.redis import get_client

PER_IP_LIMIT = 10
PER_IP_WINDOW_SECONDS = 60
PER_POD_LIMIT = 100
PER_POD_WINDOW_SECONDS = 60 * 60


@dataclass(frozen=True)
class RateLimitedError(Exception):
    """Raised when an identity-exchange attempt exceeds a window."""

    axis: str  # "ip" or "pod"
    limit: int
    window_seconds: int

    def __str__(self) -> str:
        return f"rate_limited:{self.axis}"


async def _hit(key: str, limit: int, window_seconds: int) -> None:
    """Sliding-window approximation via Redis ZSET.

    Each call:
    1. Trim entries older than the window.
    2. Count current entries.
    3. If >= limit, raise.
    4. Otherwise add the current timestamp (score=now, member=now).
    5. Set key TTL = window_seconds (refresh).

    Approximate (sub-second resolution; multiple processes can race past
    the boundary by one each) — exact accuracy isn't worth a Lua script
    at POC scale.
    """
    redis = get_client()
    now = time.time()
    cutoff = now - window_seconds
    await redis.zremrangebyscore(key, "-inf", cutoff)
    count = await redis.zcard(key)
    if count >= limit:
        # Determine axis from key prefix for the error message.
        axis = "ip" if ":ip:" in key else "pod"
        raise RateLimitedError(axis=axis, limit=limit, window_seconds=window_seconds)
    await redis.zadd(key, {f"{now:.6f}:{count}": now})
    await redis.expire(key, window_seconds)


async def check_identity_exchange(*, source_ip: str | None, agent_pod_id: str | None) -> None:
    """Apply both per-IP and per-pod limits. Raises `RateLimitedError`
    on first violation; checks IP before pod so a flood from one IP
    can't mask a single legitimate pod's rate.

    `source_ip` may be None if the request has no client (test harness);
    in that case the IP axis is skipped. Same for `agent_pod_id`.
    """
    if source_ip:
        await _hit(f"rl:identity_exchange:ip:{source_ip}", PER_IP_LIMIT, PER_IP_WINDOW_SECONDS)
    if agent_pod_id:
        await _hit(f"rl:identity_exchange:pod:{agent_pod_id}", PER_POD_LIMIT, PER_POD_WINDOW_SECONDS)


__all__ = [
    "PER_IP_LIMIT",
    "PER_IP_WINDOW_SECONDS",
    "PER_POD_LIMIT",
    "PER_POD_WINDOW_SECONDS",
    "RateLimitedError",
    "check_identity_exchange",
]
