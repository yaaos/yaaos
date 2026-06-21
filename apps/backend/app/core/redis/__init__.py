"""core/redis — the single seam in front of Redis.

Encapsulates the Redis client: it never leaves the module. Every Redis
operation is a named primitive — the health `ping`, the JSON pub/sub bus
(`publish`/`subscribe`/`subscriber_count`), and the `sliding_window_hit`
rate-limit counter. Per-loop client cache lives in `service.py` so cross-loop
reuse doesn't fail.

The active pub/sub instance is ContextVar-bound with an eager default —
production never needs a startup bind. `set_pubsub_for_tests` is the test
seam; the `pubsub_isolation` fixture in `app/testing/isolation` uses it.
"""

from app.core.redis.hash_ops import hash_delete, hash_get_all, hash_set
from app.core.redis.pubsub import (
    publish,
    set_pubsub_for_tests,
    subscribe,
    subscriber_count,
)
from app.core.redis.pubsub import shutdown as _bus_shutdown
from app.core.redis.service import delete_keys_with_prefix, ping, scan_keys
from app.core.redis.service import shutdown as _client_shutdown
from app.core.redis.set_if_absent import set_if_absent
from app.core.redis.set_ops import set_add, set_members, set_remove
from app.core.redis.sliding_window import sliding_window_hit
from app.core.redis.zset_ops import (
    zset_add_member,
    zset_card,
    zset_members,
    zset_remove_by_score,
    zset_remove_member,
)
from app.core.shutdown_registry import register_web_shutdown_hook, register_worker_shutdown_hook


async def shutdown() -> None:
    """Close every cached client and drop the pub/sub instance. Registered
    on both web and worker shutdown registries. Idempotent."""
    await _client_shutdown()
    await _bus_shutdown()


__all__ = [
    "delete_keys_with_prefix",
    "hash_delete",
    "hash_get_all",
    "hash_set",
    "ping",
    "publish",
    "scan_keys",
    "set_add",
    "set_if_absent",
    "set_members",
    "set_pubsub_for_tests",
    "set_remove",
    "shutdown",
    "sliding_window_hit",
    "subscribe",
    "subscriber_count",
    "zset_add_member",
    "zset_card",
    "zset_members",
    "zset_remove_by_score",
    "zset_remove_member",
]

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
