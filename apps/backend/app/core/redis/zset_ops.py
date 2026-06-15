"""ZSET primitives for cross-pod subscriber presence tracking.

Members are scored by Unix timestamp — callers can sweep stale entries
with `zset_remove_by_score` using a `now - threshold` bound.
"""

from __future__ import annotations

from app.core.redis.service import _get_client


async def zset_add_member(key: str, member: str, score: float) -> None:
    """Add `member` to the sorted set at `key` with `score`.

    Wraps ZADD. Idempotent on (key, member) — re-adding the same member
    updates its score.
    """
    await _get_client().zadd(key, {member: score})


async def zset_remove_member(key: str, member: str) -> int:
    """Remove `member` from the sorted set at `key`.

    Wraps ZREM. Returns 1 if the member was present and removed, 0 if absent.
    """
    result = await _get_client().zrem(key, member)
    return int(result)


async def zset_card(key: str) -> int:
    """Return the cardinality of the sorted set at `key`.

    Wraps ZCARD. Returns 0 when the key is absent.
    """
    result = await _get_client().zcard(key)
    return int(result)


async def zset_remove_by_score(key: str, min_score: float, max_score: float) -> int:
    """Remove all members of the sorted set at `key` with score in [min, max].

    Wraps ZREMRANGEBYSCORE on an exact key. Returns the count removed.
    The sweeper iterates workflow_subscribers:* via Redis SCAN and calls
    this per key — Redis does not support glob ranges on ZREMRANGEBYSCORE.
    """
    result = await _get_client().zremrangebyscore(key, min_score, max_score)
    return int(result)
