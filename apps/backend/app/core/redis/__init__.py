"""core/redis — the single seam in front of Redis.

Encapsulates the Redis client: it never leaves the module. Every Redis
operation is a named primitive — the health `ping`, the JSON pub/sub bus
(`publish`/`subscribe`/`subscriber_count`), and the `sliding_window_hit`
rate-limit counter. Per-loop client cache lives in `service.py` so cross-loop
reuse doesn't fail.

The active `RedisPubsub` instance is ContextVar-bound. `bind_pubsub` is the
production DI seam — the composition root calls it at startup; the
`pubsub_isolation` fixture in `app/testing/isolation` calls it per test.
"""

from app.core.redis.pubsub import (
    RedisPubsub,
    bind_pubsub,
    publish,
    subscribe,
    subscriber_count,
)
from app.core.redis.pubsub import shutdown as _bus_shutdown
from app.core.redis.service import delete_keys_matching, ping
from app.core.redis.service import shutdown as _client_shutdown
from app.core.redis.sliding_window import sliding_window_hit
from app.core.shutdown_registry import register_web_shutdown_hook, register_worker_shutdown_hook


async def shutdown() -> None:
    """Close every cached client and drop the pub/sub instance. Registered
    on both web and worker shutdown registries. Idempotent."""
    await _client_shutdown()
    await _bus_shutdown()


__all__ = [
    "RedisPubsub",
    "bind_pubsub",
    "delete_keys_matching",
    "ping",
    "publish",
    "shutdown",
    "sliding_window_hit",
    "subscribe",
    "subscriber_count",
]

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
