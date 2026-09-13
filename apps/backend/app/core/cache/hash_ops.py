"""Private Redis record representation: partial HASH updates and UTF-8 fields."""

from __future__ import annotations

from redis.asyncio import Redis


async def _update_record(client: Redis, key: str, *, fields: dict[str, str]) -> None:
    if fields:
        await client.hset(key, mapping=fields)


async def _read_record(client: Redis, key: str) -> dict[str, str]:
    raw = await client.hgetall(key)
    return {field.decode(): value.decode() for field, value in raw.items()}
