"""SET NX primitive for cross-pod idempotency and replay protection.

A single-operation primitive: atomically set a key iff it is absent.
Redis `SET key 1 NX EX ttl` guarantees at-most-once semantics across
all pods — the first writer wins, all concurrent or later writers see
`False`. TTL is the caller-supplied window; keys expire automatically.
"""

from __future__ import annotations

from app.core.redis.service import _get_client


async def set_if_absent(key: str, ttl_seconds: int) -> bool:
    """Atomically set *key* with *ttl_seconds* expiry iff the key is absent.

    Wraps `SET key 1 NX EX ttl_seconds`. Returns `True` when the key was
    inserted (this caller wins), `False` when it already existed (replay /
    duplicate). redis-py returns `True` on insert and `None` on conflict.
    """
    result = await _get_client().set(name=key, value="1", nx=True, ex=ttl_seconds)
    return result is True
