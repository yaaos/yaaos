"""JSON pub/sub bus over Redis.

Raw bytes primitives (`_publish_bytes` / `_subscribe_bytes`) route through
`service._get_client` so the underlying client is the loop-bound singleton —
no per-call client churn. The per-iterator `pubsub` object is a thin
Redis-side wrapper that holds the subscription registration; it lives for the
iterator's lifetime and tears down on iterator exit.

`_RedisPubsub` layers JSON encode/decode on top: callers publish/subscribe
`dict` events, never bytes. Channel naming and event semantics belong to
consumers (`core/sse`) — this module is naming-agnostic. `subscriber_count`
is local-process — Redis's `PUBSUB NUMSUB` is cluster-wide and not what
callers want.

The active instance is held in a ContextVar with an eager default so production
never needs a startup bind call. `set_pubsub_for_tests` is the test seam: it
binds a fresh instance (optionally recording) for the duration of the `with`
block and restores the prior binding on exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

import structlog

from app.core.redis.service import _get_client

log = structlog.get_logger("core.redis.pubsub")


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


class _RedisPubsub:
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


class _RecordingRedisPubsub(_RedisPubsub):
    """Test-only variant that records published events without connecting to Redis.

    Use via `set_pubsub_for_tests(scenario="recording")`. After the `with`
    block, inspect `instance.published` for the list of `(channel, event)` pairs.
    """

    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        self.published.append((channel, event))
        return 0


_PUBSUB_SCENARIOS: dict[str, type[_RedisPubsub]] = {
    "default": _RedisPubsub,
    "recording": _RecordingRedisPubsub,
}

_pubsub_var: ContextVar[_RedisPubsub | None] = ContextVar("_pubsub_var", default=None)


def _get() -> _RedisPubsub:
    val = _pubsub_var.get()
    if val is None:
        val = _RedisPubsub()
        _pubsub_var.set(val)
    return val


@contextmanager
def set_pubsub_for_tests(
    *,
    scenario: Literal["default", "recording"] = "default",
) -> Iterator[_RedisPubsub]:
    """Context manager: bind a fresh pub/sub instance for the duration.

    Restores the prior binding on exit — even on exception. The `recording`
    scenario yields a `_RecordingRedisPubsub` whose `.published` list captures
    every `(channel, event)` pair without connecting to Redis; use it to assert
    on published events in service tests.
    """
    instance = _PUBSUB_SCENARIOS[scenario]()
    token = _pubsub_var.set(instance)
    try:
        yield instance
    finally:
        _pubsub_var.reset(token)


async def shutdown() -> None:
    """Close the active bus instance. Called by web- and worker-process shutdown registries."""
    await _get().aclose()


async def publish(channel: str, event: dict[str, Any]) -> int:
    """Publish `event` (JSON-serialized) on `channel`. Returns the cluster-wide
    delivery count; 0 when nobody is listening."""
    return await _get().publish(channel, event)


def subscribe(
    channel: str,
    on_subscribed: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over JSON events on `channel`.

    `on_subscribed`, when provided, is set after the Redis SUBSCRIBE handshake
    completes — before any message is delivered.
    """
    return _get().subscribe(channel, on_subscribed=on_subscribed)


def subscriber_count(channel: str) -> int:
    """Local-process subscriber count for `channel` — diagnostics only,
    not cluster-wide."""
    return _get().subscriber_count(channel)
