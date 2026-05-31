"""Centralized Redis client construction + health check.

redis-py's async client binds its connection pool to the event loop where
the first command ran. Reusing one client across loops (web request loop
vs worker loop vs TestClient portal loop) fails with a "Future attached to
a different loop" error. We cache one client per running loop, keyed by
`id(asyncio.get_running_loop())`, so cross-loop callers each get their own
client transparently.

The client never leaves the module: `_get_client()` is private. Every Redis
operation is exposed as a named primitive — `ping` here, the JSON pub/sub
bus in [`pubsub.py`](pubsub.py), the rate-limit counter in
[`sliding_window.py`](sliding_window.py).
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog
from redis.asyncio import Redis, from_url

from app.core.config import get_settings

log = structlog.get_logger("core.redis")

# One client per running event loop. Keyed by `id(loop)` so a fresh loop
# (e.g. a TestClient portal) gets its own client rather than reusing one
# bound to a different loop's selector.
_clients: dict[int, Redis] = {}


def _get_client() -> Redis:
    """Return the Redis client bound to the current running loop. Private —
    the client never crosses the module boundary.

    Construction is lazy — first call per loop opens a client; subsequent
    calls in the same loop return the cached instance. No connection is
    opened until the first command runs.

    Bytes everywhere (`decode_responses=False`). The JSON pub/sub bus in
    `pubsub.py` owns encode/decode.
    """
    loop_id = id(asyncio.get_running_loop())
    client = _clients.get(loop_id)
    if client is None:
        client = from_url(get_settings().redis_url, decode_responses=False)
        _clients[loop_id] = client
    return client


async def delete_keys_matching(pattern: str) -> int:
    """Delete all keys matching `pattern`. Returns count deleted.

    Non-prod utility exposed for test teardown (e.g. rate-limit key cleanup).
    Callers must ensure they only pass trusted patterns — this is a bulk delete.
    """
    redis = _get_client()
    keys = await redis.keys(pattern)
    if not keys:
        return 0
    return await redis.delete(*keys)


async def ping() -> bool:
    """`PING` against Redis. Returns True on success, False on any error.
    Used by `/api/health` alongside `core/database.ping()`. Swallows all
    exceptions — the endpoint reports a boolean, not a stack trace."""
    try:
        await _get_client().ping()
        return True
    except Exception:
        return False


async def shutdown() -> None:
    """Close every cached client. Called by the process shutdown registries
    during web/worker teardown and from test teardown. Idempotent —
    re-running after everything is closed is a no-op."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        with contextlib.suppress(Exception):
            await client.aclose()


def _reset_clients_for_tests() -> None:
    """Drop the per-loop client cache without closing. Intra-module test
    helper — reach for it via direct submodule import from this module's
    own `test/` directory. Not part of the public interface."""
    _clients.clear()
