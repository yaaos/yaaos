"""HASH primitives for cross-pod workflow route metadata.

Stores the `{workspace_id, agent_id}` mapping for a workflow execution so
any pod can resolve which agent's WS channel to notify on subscribe/unsubscribe.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.core.redis.service import _get_client


async def hash_set(key: str, fields: Mapping[str, str]) -> None:
    """Set multiple fields on the hash at `key`.

    Wraps HSET with multi-field shape. Overwrites existing fields.
    """
    await _get_client().hset(key, mapping=dict(fields))  # type: ignore[arg-type]


async def hash_get_all(key: str) -> dict[str, str]:
    """Return all fields and values of the hash at `key`.

    Wraps HGETALL. Returns an empty dict when the key is absent.
    """
    raw = await _get_client().hgetall(key)
    return {
        k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
        for k, v in raw.items()
    }


async def hash_delete(key: str) -> None:
    """Delete the entire hash at `key`.

    Wraps DEL. No-op when the key is absent.
    """
    await _get_client().delete(key)
