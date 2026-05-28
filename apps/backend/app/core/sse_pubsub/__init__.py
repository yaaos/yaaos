"""core/sse_pubsub — Redis-backed pub/sub for ActivityEvent fanout.

Backed by Redis `PUBLISH`/`SUBSCRIBE` so a publish from the worker process
reaches an SSE subscriber attached to a different web process. Channel
name shape: `activity:{workflow_execution_id}`.
"""

from app.core.sse_pubsub.service import (
    RedisPubsub,
    channel_for,
    get_pubsub,
    publish,
    reset_pubsub,
    shutdown,
    subscribe,
    subscriber_count,
)

__all__ = [
    "RedisPubsub",
    "channel_for",
    "get_pubsub",
    "publish",
    "reset_pubsub",
    "shutdown",
    "subscribe",
    "subscriber_count",
]

from app.core.shutdown_registry import register_web_shutdown_hook

register_web_shutdown_hook(shutdown)
