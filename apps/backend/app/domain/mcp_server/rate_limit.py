"""Rate limiting for `POST /api/mcp-server/register`.

Dynamic client registration (RFC 7591) is unauthenticated by design, so any
caller can create `mcp_oauth_clients` rows. Two sliding-window counters per
source IP, backed by Redis, keep bulk row-creation impractical:

  Burst:     3 registrations / 60 seconds
  Sustained: 10 registrations / 3600 seconds

Both windows must pass. Registration is a once-per-client-install action, so
even a whole office behind one NAT address stays well inside these limits.

On a violation, raise `RateLimitedError` carrying the violated window; the
endpoint maps it to HTTP 429 + `Retry-After: <window_seconds>`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.redis import delete_keys_with_prefix, sliding_window_hit

# Redis key prefix for all client-registration rate-limit windows. Distinct
# from `core/agent_gateway`'s identity-exchange prefix — separate surfaces.
KEY_PREFIX = "rl:mcp_register:"

BURST_LIMIT = 3
BURST_WINDOW_SECONDS = 60

SUSTAINED_LIMIT = 10
SUSTAINED_WINDOW_SECONDS = 3600


@dataclass
class RateLimitedError(Exception):
    """Raised when a registration attempt exceeds one of the windows."""

    axis: str  # "ip:burst" | "ip:sustained"
    limit: int
    window_seconds: int

    def __str__(self) -> str:
        return f"rate_limited:{self.axis}"


def _burst_key(source_ip: str) -> str:
    return f"{KEY_PREFIX}burst:ip:{source_ip}"


def _sustained_key(source_ip: str) -> str:
    return f"{KEY_PREFIX}sustained:ip:{source_ip}"


async def _hit(key: str, *, axis: str, limit: int, window_seconds: int) -> None:
    """Record a hit via `core/redis.sliding_window_hit`; raise `RateLimitedError`
    when the window is already at its limit."""
    if not await sliding_window_hit(key, limit=limit, window_seconds=window_seconds):
        raise RateLimitedError(axis=axis, limit=limit, window_seconds=window_seconds)


async def check_register(*, source_ip: str | None) -> None:
    """Apply both per-IP windows. Raises `RateLimitedError` on violation.

    `source_ip` may be None when the request has no client (test harness);
    in that case the IP axis is skipped.
    """
    if not source_ip:
        return
    await _hit(
        _burst_key(source_ip),
        axis="ip:burst",
        limit=BURST_LIMIT,
        window_seconds=BURST_WINDOW_SECONDS,
    )
    await _hit(
        _sustained_key(source_ip),
        axis="ip:sustained",
        limit=SUSTAINED_LIMIT,
        window_seconds=SUSTAINED_WINDOW_SECONDS,
    )


async def delete_rate_limits() -> int:
    """Delete all client-registration rate-limit keys from Redis. Returns the
    number of keys deleted. Used by the per-test flush fixture."""
    return await delete_keys_with_prefix(KEY_PREFIX)


__all__ = [
    "BURST_LIMIT",
    "BURST_WINDOW_SECONDS",
    "SUSTAINED_LIMIT",
    "SUSTAINED_WINDOW_SECONDS",
    "RateLimitedError",
    "check_register",
    "delete_rate_limits",
]
