"""Rate limiting for `/identity/exchange`.

Sliding-window counter per source IP, backed by Redis. Burst guard against
credential-stuffing and STS-replay flooding.

Limit: 10 attempts / minute per source IP.

On limit hit, raise `RateLimitedError` with axis="ip"; the endpoint maps it
to HTTP 429 with `identity_exchange.rate_limited` audit.

Note: the limit is intentionally kept at 10/min per IP. Multiple agent
instances behind the same NAT/ALB share this window. If ops observe false
throttles on high-scale deployments, raise `PER_IP_LIMIT` (or add a
per-org signed-token allowance) before deployment.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.redis import sliding_window_hit

# Redis key prefix for all identity-exchange rate-limit windows.
KEY_PREFIX = "rl:identity_exchange:"

PER_IP_LIMIT = 10
PER_IP_WINDOW_SECONDS = 60


@dataclass
class RateLimitedError(Exception):
    """Raised when an identity-exchange attempt exceeds a window."""

    axis: str  # "ip"
    limit: int
    window_seconds: int

    def __str__(self) -> str:
        return f"rate_limited:{self.axis}"


async def _hit(key: str, limit: int, window_seconds: int) -> None:
    """Record a rate-limit hit via `core/redis.sliding_window_hit`; raise
    `RateLimitedError` (axis="ip") when the window is already at its limit."""
    if not await sliding_window_hit(key, limit=limit, window_seconds=window_seconds):
        raise RateLimitedError(axis="ip", limit=limit, window_seconds=window_seconds)


async def check_identity_exchange(*, source_ip: str | None) -> None:
    """Apply the per-IP rate limit. Raises `RateLimitedError` on violation.

    `source_ip` may be None when the request has no client (test harness);
    in that case the IP axis is skipped.
    """
    if source_ip:
        await _hit(f"{KEY_PREFIX}ip:{source_ip}", PER_IP_LIMIT, PER_IP_WINDOW_SECONDS)


__all__ = [
    "PER_IP_LIMIT",
    "PER_IP_WINDOW_SECONDS",
    "RateLimitedError",
    "check_identity_exchange",
]
