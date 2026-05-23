"""core/redis — Redis client + pub/sub primitives.

Single access point for Redis across the app. Owns connection management
(per-loop client cache so cross-loop reuse doesn't fail), the URL accessor
that `core/tasks/broker.py` passes to taskiq, raw pub/sub helpers that
`core/sse_pubsub` builds on, and the health-check ping.
"""

from app.core.redis.pubsub import publish, subscribe
from app.core.redis.service import _reset_for_tests, aclose, get_client, get_url, ping

__all__ = [
    "_reset_for_tests",
    "aclose",
    "get_client",
    "get_url",
    "ping",
    "publish",
    "subscribe",
]
