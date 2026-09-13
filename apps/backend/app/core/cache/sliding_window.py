"""Private Redis approximate sliding window; acceptance alone refreshes expiry."""

from __future__ import annotations

from redis.asyncio import Redis


async def _allow_hit(client: Redis, key: str, *, limit: int, window_seconds: int, now: float) -> bool:
    # Intentionally non-atomic: concurrent processes can exceed the limit.
    await client.zremrangebyscore(key, "-inf", now - window_seconds)
    count = await client.zcard(key)
    if count >= limit:
        return False
    await client.zadd(key, {f"{now:.6f}:{count}": now})
    await client.expire(key, window_seconds)
    return True
