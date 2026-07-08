"""SET primitives for the per-agent run-route index.

The `agent_routes:{agent_id}` SET holds all run_ids the agent is
currently expected to stream. When an agent's WS reconnects to a new pod,
that pod reads this SET to know which subscribes to re-emit.
"""

from __future__ import annotations

from app.core.redis.service import _get_client


async def set_add(key: str, member: str) -> None:
    """Add `member` to the set at `key`.

    Wraps SADD. Idempotent — adding a member that already exists is a no-op.
    """
    await _get_client().sadd(key, member)


async def set_remove(key: str, member: str) -> None:
    """Remove `member` from the set at `key`.

    Wraps SREM. No-op when the member is absent.
    """
    await _get_client().srem(key, member)


async def set_members(key: str) -> set[str]:
    """Return all members of the set at `key`.

    Wraps SMEMBERS. Returns an empty set when the key is absent.
    """
    raw = await _get_client().smembers(key)
    return {v.decode() if isinstance(v, bytes) else v for v in raw}
