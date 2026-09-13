"""Private Redis atomic expiring claim; conflicts never refresh the TTL."""

from __future__ import annotations

from redis.asyncio import Redis


async def _claim_once(client: Redis, key: str, *, ttl_seconds: int) -> bool:
    return await client.set(name=key, value="1", nx=True, ex=ttl_seconds) is True
