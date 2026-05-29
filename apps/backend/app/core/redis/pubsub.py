"""JSON pub/sub bus over Redis.

Raw bytes primitives (`_publish_bytes` / `_subscribe_bytes`) route through
`service._get_client` so the underlying client is the loop-bound singleton —
no per-call client churn. The per-iterator `pubsub` object is a thin
Redis-side wrapper that holds the subscription registration; it lives for the
iterator's lifetime and tears down on iterator exit.

`RedisPubsub` layers JSON encode/decode on top: callers publish/subscribe
`dict` events, never bytes. Channel naming and event semantics belong to
consumers (`core/sse`) — this module is naming-agnostic. `subscriber_count`
is local-process — Redis's `PUBSUB NUMSUB` is cluster-wide and not what
callers want. The process-singleton (`get_pubsub`) holds the count state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import structlog

from app.core.redis.service import _get_client

log = structlog.get_logger("core.redis.pubsub")


async def _publish_bytes(channel: str, payload: bytes) -> int:
    """Publish raw `payload` on `channel`. Returns the cluster-wide delivery
    count (number of subscribers Redis routed the message to)."""
    n = await _get_client().publish(channel, payload)
    return int(n)


async def _subscribe_bytes(channel: str) -> AsyncIterator[bytes]:
    """Async iterator over message bodies on `channel`. Registers a Redis
    subscription on first iteration; unregisters when the iterator exits
    (consumer cancellation, exhaustion, or context exit).

    Filters out Redis's own subscribe/unsubscribe confirmation messages —
    callers only see real `message` payloads. Yields raw bytes.
    """
    client = _get_client()
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for msg in pubsub.listen():
            if msg.get("type") != b"message" and msg.get("type") != "message":
                continue
            data = msg.get("data")
            if isinstance(data, (bytes, bytearray)):
                yield bytes(data)
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel)
        with contextlib.suppress(Exception):
            await pubsub.aclose()


class RedisPubsub:
    """JSON pub/sub bus. Channel-agnostic — callers pass channel names and
    `dict` events; this layer owns JSON encode/decode. `subscriber_count`
    is local-process — Redis's `PUBSUB NUMSUB` is cluster-wide and not what
    callers want.
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
        return await _publish_bytes(channel, payload)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over events on `channel`. Local subscriber count
        is incremented on entry, decremented on iterator close."""
        async with self._lock:
            self._local_counts[channel] = self._local_counts.get(channel, 0) + 1
        try:
            async for payload in _subscribe_bytes(channel):
                try:
                    yield json.loads(payload.decode())
                except json.JSONDecodeError:
                    log.warning("pubsub.malformed_payload", channel=channel)
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
    """Process-singleton pub/sub bus. Holds the local subscriber-count state."""
    global _singleton
    if _singleton is None:
        _singleton = RedisPubsub()
    return _singleton


async def shutdown() -> None:
    """Drop the singleton. Called by web- and worker-process shutdown registries."""
    global _singleton
    if _singleton is not None:
        await _singleton.aclose()
    _singleton = None


def reset_pubsub() -> None:
    """Drop the singleton synchronously. For test isolation only — production
    code uses the async `shutdown()` via the process shutdown registries."""
    global _singleton
    _singleton = None


async def publish(channel: str, event: dict[str, Any]) -> int:
    """Publish `event` (JSON-serialized) on `channel`. Returns the cluster-wide
    delivery count; 0 when nobody is listening."""
    return await get_pubsub().publish(channel, event)


def subscribe(channel: str) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over JSON events on `channel`."""
    return get_pubsub().subscribe(channel)


def subscriber_count(channel: str) -> int:
    """Local-process subscriber count for `channel` — diagnostics only,
    not cluster-wide."""
    return get_pubsub().subscriber_count(channel)
