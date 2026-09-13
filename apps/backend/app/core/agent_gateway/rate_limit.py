"""Rate limiting for `/identity/exchange`.

Shared sliding-window counter per source IP, backed by core/cache. Burst guard against
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

from app.core.cache import allow_hit, clear_prefix

# Cache key prefix for all identity-exchange rate-limit windows.
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
    """Admit a hit via core/cache; raise when the window is already at its limit."""
    if not await allow_hit(key, limit=limit, window_seconds=window_seconds):
        raise RateLimitedError(axis="ip", limit=limit, window_seconds=window_seconds)


async def check_identity_exchange(*, source_ip: str | None) -> None:
    """Apply the per-IP rate limit. Raises `RateLimitedError` on violation.

    `source_ip` may be None when the request has no client (test harness);
    in that case the IP axis is skipped.
    """
    if source_ip:
        await _hit(f"{KEY_PREFIX}ip:{source_ip}", PER_IP_LIMIT, PER_IP_WINDOW_SECONDS)


async def delete_rate_limits() -> int:
    """Clear the identity-exchange rate-limit namespace.

    Used by the test-reset path to flush the agent IP's sliding-window
    counters between test runs so a fresh seed isn't blocked by a prior
    run's burst. Returns the number of keys deleted.
    """
    return await clear_prefix(KEY_PREFIX)


__all__ = [
    "PER_IP_LIMIT",
    "PER_IP_WINDOW_SECONDS",
    "RateLimitedError",
    "check_identity_exchange",
    "delete_rate_limits",
]
