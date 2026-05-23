"""Raw Redis pub/sub primitives. No channel-naming opinions; no payload
encoding. Consumers (`core/sse_pubsub`) layer their semantics on top.

Both `publish` and `subscribe` route through `core/redis.service.get_client`
so the underlying client is the loop-bound singleton — no per-call client
churn. The per-iterator `pubsub` object is a thin Redis-side wrapper that
holds the subscription registration; it lives for the iterator's lifetime
and tears down on iterator exit.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import structlog

from app.core.redis.service import get_client

log = structlog.get_logger("core.redis.pubsub")


async def publish(channel: str, payload: bytes) -> int:
    """Publish `payload` on `channel`. Returns the cluster-wide delivery
    count (number of subscribers Redis routed the message to). Payload is
    raw bytes; callers encode."""
    n = await get_client().publish(channel, payload)
    return int(n)


async def subscribe(channel: str) -> AsyncIterator[bytes]:
    """Async iterator over message bodies on `channel`. Registers a Redis
    subscription on first iteration; unregisters when the iterator exits
    (consumer cancellation, exhaustion, or context exit).

    Filters out Redis's own subscribe/unsubscribe confirmation messages —
    callers only see real `message` payloads. Yields raw bytes.
    """
    client = get_client()
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
