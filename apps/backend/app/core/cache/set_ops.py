"""Private Redis key-set enumeration; public prefixes are literal, never globs."""

from __future__ import annotations

from redis.asyncio import Redis


def _prefix_pattern(prefix: str) -> str:
    return "".join("\\" + char if char in "\\*?[]" else char for char in prefix) + "*"


async def _list_keys(client: Redis, *, prefix: str) -> list[str]:
    keys: set[str] = set()
    async for raw in client.scan_iter(match=_prefix_pattern(prefix), count=100):
        key = raw.decode() if isinstance(raw, bytes) else raw
        # SCAN may repeat keys; the literal check also protects deletion callers.
        if key.startswith(prefix):
            keys.add(key)
    return sorted(keys)


async def _clear_prefix(client: Redis, prefix: str) -> int:
    deleted = 0
    for key in await _list_keys(client, prefix=prefix):
        deleted += int(await client.delete(key))
    return deleted
