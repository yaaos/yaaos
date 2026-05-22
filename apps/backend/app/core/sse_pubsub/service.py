"""Pub/sub primitive for ActivityEvent fanout.

Two backends:

- `InMemoryPubsub` — default. asyncio.Queue per channel, lives in the
  current process. Adequate for single-instance backends + every test.
- (Future) Redis backend, wired when `settings.redis_url` is set. The
  channel-naming + API surface match; swapping backends is a single
  setting flip. M05 Phase 8b foundations ships only the in-memory
  implementation; production wiring of the Redis backend lands in the
  follow-on once the worker process owns its broker setup.

Channel naming convention: `activity:{workflow_execution_id}`. The
caller is responsible for forming the key. The SSE handler in `web.py`
subscribes per workflow execution; `core/agent_gateway` publishes from
the WebSocket ingress.

The Pydantic-encoded payload crosses the seam as a `dict[str, Any]`;
the channel name discriminates routing. No per-event ack; activity is
fire-and-forget per architecture.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import structlog

log = structlog.get_logger("core.sse_pubsub")


class InMemoryPubsub:
    """Process-local pub/sub. One asyncio.Queue per (channel, subscriber)
    so each subscriber sees its own copy of every event.

    Bounded queue keeps a slow subscriber from accumulating unbounded
    backlog: when the queue is full the oldest event is dropped (best-
    effort delivery per the architecture's persistence-invariant note).
    """

    def __init__(self, *, per_subscriber_buffer: int = 256) -> None:
        self._channels: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._buffer = per_subscriber_buffer

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        """Fan out `event` to every current subscriber on `channel`.
        Returns the number of subscribers it reached. Drops the oldest
        queued event on any subscriber that's at capacity."""
        async with self._lock:
            subs = list(self._channels.get(channel, ()))
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the head — best-effort delivery.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)
        return len(subs)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over events on `channel`. The subscriber is
        registered on first iteration and removed when the iterator is
        closed (consumer cancellation, exhaustion, or context exit)."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._buffer)
        async with self._lock:
            self._channels.setdefault(channel, set()).add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            async with self._lock:
                subs = self._channels.get(channel)
                if subs is not None:
                    subs.discard(queue)
                    if not subs:
                        self._channels.pop(channel, None)

    def subscriber_count(self, channel: str) -> int:
        """Diagnostic — number of subscribers currently attached. Tests
        use this to assert demand-pull semantics."""
        return len(self._channels.get(channel, ()))


_singleton: InMemoryPubsub | None = None


def get_pubsub() -> InMemoryPubsub:
    """Process-singleton. The Redis-backed variant slots in here later."""
    global _singleton
    if _singleton is None:
        _singleton = InMemoryPubsub()
    return _singleton


def _reset_for_tests() -> None:
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
    """Channel key used by `core/agent_gateway` publishers and SSE
    subscribers in `web.py`. Centralized so the naming convention stays
    consistent across both sides of the fanout."""
    return f"activity:{workflow_execution_id}"


def subscriber_count(channel: str) -> int:
    return get_pubsub().subscriber_count(channel)
