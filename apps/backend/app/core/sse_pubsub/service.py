"""ActivityEvent fanout — channel naming + JSON encode/decode over `core/redis`.

Publishers call `publish(channel, event)` with `channel =
activity:{workflow_execution_id}`; subscribers iterate `async for event in
subscribe(channel)`. Backed by Redis `PUBLISH` / `SUBSCRIBE` via
[`core/redis`](../redis/__init__.py) so a publish from the worker process
reaches an SSE subscriber on the web process. Fire-and-forget per Redis
semantics — slow consumers do not backpressure publishers.

Channel naming convention: `activity:{workflow_execution_id}`. Caller
forms the key via `channel_for()`. The SSE handler in `web.py` subscribes
per workflow execution; `core/agent_gateway` (and the reviewer's direct
activity publisher) publish.

The Pydantic-encoded payload crosses the seam as a `dict[str, Any]`
serialized to JSON; this module owns the encode/decode. `core/redis`
handles connection management — there's no Redis client construction
here anymore.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog

from app.core import redis as redis_client

log = structlog.get_logger("core.sse_pubsub")


class RedisPubsub:
    """ActivityEvent pub/sub. Channel naming + JSON encode/decode on top
    of `core/redis`. `subscriber_count` is local-process — Redis's
    `PUBSUB NUMSUB` is cluster-wide and not what callers want.
    """

    def __init__(self) -> None:
        self._local_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        self._local_counts.clear()

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        """Publish `event` (JSON-serialized) on `channel`. Returns the
        cluster-wide delivery count. Returns 0 when nobody is listening."""
        payload = json.dumps(event).encode()
        return await redis_client.publish(channel, payload)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over events on `channel`. Local subscriber count
        is incremented on entry, decremented on iterator close."""
        async with self._lock:
            self._local_counts[channel] = self._local_counts.get(channel, 0) + 1
        try:
            async for payload in redis_client.subscribe(channel):
                try:
                    yield json.loads(payload.decode())
                except json.JSONDecodeError:
                    log.warning("sse_pubsub.malformed_payload", channel=channel)
                    continue
        finally:
            async with self._lock:
                cur = self._local_counts.get(channel, 0)
                if cur <= 1:
                    self._local_counts.pop(channel, None)
                else:
                    self._local_counts[channel] = cur - 1

    def subscriber_count(self, channel: str) -> int:
        """Local-process subscriber count for diagnostics / tests."""
        return self._local_counts.get(channel, 0)


_singleton: RedisPubsub | None = None


def get_pubsub() -> RedisPubsub:
    """Process-singleton pub/sub."""
    global _singleton
    if _singleton is None:
        _singleton = RedisPubsub()
    return _singleton


async def shutdown() -> None:
    """Drop the singleton. Called by the web-process shutdown registry."""
    global _singleton
    if _singleton is not None:
        await _singleton.aclose()
    _singleton = None


def reset_pubsub() -> None:
    """Drop the singleton synchronously. For test isolation only — production
    code uses the async `shutdown()` via the web shutdown registry."""
    global _singleton
    _singleton = None


async def publish(channel: str, event: dict[str, Any]) -> int:
    """Module-level convenience: publish to the process singleton."""
    return await get_pubsub().publish(channel, event)


def subscribe(channel: str) -> AsyncIterator[dict[str, Any]]:
    """Module-level convenience: subscribe via the process singleton.

    Returns an async iterator, not a coroutine — consumers do
    `async for event in subscribe(...)`.
    """
    return get_pubsub().subscribe(channel)


def channel_for(workflow_execution_id: str) -> str:
    """Channel key used by publishers and SSE subscribers. Centralized so
    the naming convention stays consistent across both sides of the
    fanout.
    """
    return f"activity:{workflow_execution_id}"


def subscriber_count(channel: str) -> int:
    return get_pubsub().subscriber_count(channel)
