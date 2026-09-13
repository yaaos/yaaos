"""Private Redis membership representation: timestamps on ZSET members."""

from __future__ import annotations

import math

from redis.asyncio import Redis

from app.core.cache.types import CacheError, Membership


async def _list_members(client: Redis, key: str) -> list[Membership]:
    raw = await client.zrange(key, 0, -1, withscores=True)
    members = []
    for member, timestamp in raw:
        if not math.isfinite(timestamp) or timestamp < 0:
            raise CacheError("Incompatible cache value")
        members.append(Membership(member.decode(), float(timestamp)))
    return members
