"""core/redis — Redis client + pub/sub primitives.

Single access point for Redis across the app. Owns connection management
(per-loop client cache so cross-loop reuse doesn't fail), the URL accessor
that `core/tasks/broker.py` passes to taskiq, raw pub/sub helpers that
`core/sse_pubsub` builds on, and the health-check ping.
"""

from app.core.redis.pubsub import publish, subscribe
from app.core.redis.service import aclose, get_client, get_url, ping, shutdown

__all__ = [
    "aclose",
    "get_client",
    "get_url",
    "ping",
    "publish",
    "shutdown",
    "subscribe",
]

from app.core.shutdown_registry import register_web_shutdown_hook, register_worker_shutdown_hook

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
