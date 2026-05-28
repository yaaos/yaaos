"""Centralized Redis client construction + health check.

redis-py's async client binds its connection pool to the event loop where
the first command ran. Reusing one client across loops (web request loop
vs worker loop vs TestClient portal loop) fails with a "Future attached to
a different loop" error. We cache one client per running loop, keyed by
`id(asyncio.get_running_loop())`, so cross-loop callers each get their own
client transparently.

Consumers (`core/sse_pubsub`, `core/tasks/broker`, future modules that
need a key/value primitive) go through `get_client()` or the higher-level
helpers in [`pubsub.py`](pubsub.py) — they never construct a Redis client
of their own.
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


def get_url() -> str:
    """Single accessor for `settings.redis_url`. Other modules go through
    this rather than reading config directly — keeps config knowledge
    contained to `core/redis`. Used by `core/tasks/broker.py` which hands
    the URL to taskiq-redis (taskiq takes a URL, not a client)."""
    return get_settings().redis_url


def get_client() -> Redis:
    """Return the Redis client bound to the current running loop.

    Construction is lazy — first call per loop opens a client; subsequent
    calls in the same loop return the cached instance. No connection is
    opened until the first command runs.

    Bytes everywhere (`decode_responses=False`). JSON encode/decode is the
    consumer's job.
    """
    loop_id = id(asyncio.get_running_loop())
    client = _clients.get(loop_id)
    if client is None:
        client = from_url(get_url(), decode_responses=False)
        _clients[loop_id] = client
    return client


async def ping() -> bool:
    """`PING` against Redis. Returns True on success, False on any error.
    Used by `/api/health` alongside `core/database.ping()`. Swallows all
    exceptions — the endpoint reports a boolean, not a stack trace."""
    try:
        await get_client().ping()
        return True
    except Exception:
        return False


async def aclose() -> None:
    """Close every cached client. Called from the worker process on
    shutdown and from test teardown. Idempotent — re-running after
    everything is closed is a no-op."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        with contextlib.suppress(Exception):
            await client.aclose()


async def shutdown() -> None:
    """Async alias for `aclose()`. Called by the process shutdown registries
    during web/worker teardown. Idempotent."""
    await aclose()


def _reset_clients_for_tests() -> None:
    """Drop the per-loop client cache without closing. Intra-module test
    helper — reach for it via direct submodule import from this module's
    own `test/` directory. Not part of the public interface."""
    _clients.clear()
