"""Sliding-window counter backed by a Redis ZSET.

A reusable rate-limit primitive: callers name a key and a window; this module
owns the ZSET mechanics. Limit *policy* (which axis, what error, what HTTP
status) stays with the caller — see `core/agent_gateway/rate_limit.py`.
"""

from __future__ import annotations

import time

from app.core.redis.service import _get_client


async def sliding_window_hit(key: str, *, limit: int, window_seconds: int) -> bool:
    """Record a hit in the sliding window at `key`, unless it would exceed `limit`.

    Each call:
    1. Trim entries older than the window.
    2. Count current entries.
    3. If >= `limit`, return False without recording.
    4. Otherwise add the current timestamp, refresh the key TTL, return True.

    Approximate (sub-second resolution; multiple processes can race past the
    boundary by one each) — exact accuracy isn't worth a Lua script at POC
    scale.
    """
    redis = _get_client()
    now = time.time()
    cutoff = now - window_seconds
    await redis.zremrangebyscore(key, "-inf", cutoff)
    count = await redis.zcard(key)
    if count >= limit:
        return False
    await redis.zadd(key, {f"{now:.6f}:{count}": now})
    await redis.expire(key, window_seconds)
    return True
