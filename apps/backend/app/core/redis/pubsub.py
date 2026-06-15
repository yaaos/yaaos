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
callers want.

The active `RedisPubsub` instance is held in a ContextVar. Production binds
the default at startup via `bind_pubsub`; the `pubsub_isolation` fixture in
`app/testing/isolation` binds a fresh instance per test. `get_pubsub()` raises
`RuntimeError` if called before any bind — deliberate fail-fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.redis.service import _get_client

log = structlog.get_logger("core.redis.pubsub")

_pubsub_var: ContextVar[RedisPubsub | None] = ContextVar("_pubsub_var", default=None)


async def _publish_bytes(channel: str, payload: bytes) -> int:
    """Publish raw `payload` on `channel`. Returns the cluster-wide delivery
    count (number of subscribers Redis routed the message to)."""
    n = await _get_client().publish(channel, payload)
    return int(n)


async def _subscribe_bytes(
    channel: str,
    on_subscribed: asyncio.Event | None = None,
) -> AsyncIterator[bytes]:
    """Async iterator over message bodies on `channel`. Registers a Redis
    subscription on first iteration; unregisters when the iterator exits
    (consumer cancellation, exhaustion, or context exit).

    Filters out Redis's own subscribe/unsubscribe confirmation messages —
    callers only see real `message` payloads. Yields raw bytes.

    `on_subscribed`, when provided, is set after `pubsub.subscribe(channel)`
    completes so callers can wait until the subscription is established before
    publishing — preventing lost messages on the fast-path.
    """
    client = _get_client()
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    if on_subscribed is not None:
        on_subscribed.set()
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

    async def subscribe(
        self,
        channel: str,
        on_subscribed: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over events on `channel`. Local subscriber count
        is incremented on entry, decremented on iterator close.

        `on_subscribed`, when provided, is set after the Redis SUBSCRIBE
        handshake completes — before the first message is delivered. Useful
        when the caller needs to guarantee no messages are lost between
        subscription setup and a subsequent publish.
        """
        async with self._lock:
            self._local_counts[channel] = self._local_counts.get(channel, 0) + 1
        try:
            async for payload in _subscribe_bytes(channel, on_subscribed=on_subscribed):
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


def bind_pubsub(instance: RedisPubsub) -> None:
    """Bind `instance` as the active pub/sub bus for the current Context.

    Called once at process startup (composition root) and once per test
    (isolation fixture). Subsequent calls in the same Context replace the
    prior binding.
    """
    _pubsub_var.set(instance)


def get_pubsub() -> RedisPubsub:
    """Return the active pub/sub bus. Raises `RuntimeError` if `bind_pubsub`
    has not been called — fail-fast so forgotten startup binds surface
    immediately rather than silently producing wrong state."""
    instance = _pubsub_var.get()
    if instance is None:
        raise RuntimeError(
            "pubsub not bound: call bind_pubsub(RedisPubsub()) at process "
            "startup or use the pubsub_isolation fixture in tests."
        )
    return instance


async def shutdown() -> None:
    """Drop the active bus instance. Called by web- and worker-process shutdown registries."""
    instance = _pubsub_var.get()
    if instance is not None:
        await instance.aclose()
    _pubsub_var.set(None)


async def publish(channel: str, event: dict[str, Any]) -> int:
    """Publish `event` (JSON-serialized) on `channel`. Returns the cluster-wide
    delivery count; 0 when nobody is listening."""
    return await get_pubsub().publish(channel, event)


def subscribe(
    channel: str,
    on_subscribed: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over JSON events on `channel`.

    `on_subscribed`, when provided, is set after the Redis SUBSCRIBE handshake
    completes — before any message is delivered.
    """
    return get_pubsub().subscribe(channel, on_subscribed=on_subscribed)


def subscriber_count(channel: str) -> int:
    """Local-process subscriber count for `channel` — diagnostics only,
    not cluster-wide."""
    return get_pubsub().subscriber_count(channel)
