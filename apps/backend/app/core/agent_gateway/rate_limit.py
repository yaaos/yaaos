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

from dataclasses import dataclass

from app.core.redis import sliding_window_hit

# Redis key prefix owned by this module for all identity-exchange rate-limit
# windows. Cross-module test machinery consumes it (never re-typing the literal).
KEY_PREFIX = "rl:identity_exchange:"

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
    """Record a rate-limit hit via `core/redis.sliding_window_hit`; raise
    `RateLimitedError` (with the axis derived from the key prefix) when the
    window is already at its limit."""
    if not await sliding_window_hit(key, limit=limit, window_seconds=window_seconds):
        axis = "ip" if ":ip:" in key else "pod"
        raise RateLimitedError(axis=axis, limit=limit, window_seconds=window_seconds)


async def check_identity_exchange(*, source_ip: str | None, agent_pod_id: str | None) -> None:
    """Apply both per-IP and per-pod limits. Raises `RateLimitedError`
    on first violation; checks IP before pod so a flood from one IP
    can't mask a single legitimate pod's rate.

    `source_ip` may be None if the request has no client (test harness);
    in that case the IP axis is skipped. Same for `agent_pod_id`.
    """
    if source_ip:
        await _hit(f"{KEY_PREFIX}ip:{source_ip}", PER_IP_LIMIT, PER_IP_WINDOW_SECONDS)
    if agent_pod_id:
        await _hit(f"{KEY_PREFIX}pod:{agent_pod_id}", PER_POD_LIMIT, PER_POD_WINDOW_SECONDS)


__all__ = [
    "KEY_PREFIX",
    "PER_IP_LIMIT",
    "PER_IP_WINDOW_SECONDS",
    "PER_POD_LIMIT",
    "PER_POD_WINDOW_SECONDS",
    "RateLimitedError",
    "check_identity_exchange",
]
