"""core/sse — Redis-backed pub/sub for ActivityEvent fanout + general events.

Backed by Redis `PUBLISH`/`SUBSCRIBE` so a publish from the worker process
reaches an SSE subscriber attached to a different web process. Two channel
shapes:
- `activity:{workflow_execution_id}` — per-workflow activity stream.
- `{org_id}:general` — org-scoped general events with typed `GeneralEventKind`.
"""

from app.core.sse.service import (
    GeneralEventKind,
    RedisPubsub,
    channel_for,
    get_pubsub,
    publish,
    publish_general,
    publish_general_after_commit,
    reset_pubsub,
    shutdown,
    subscribe,
    subscribe_general,
    subscriber_count,
)

__all__ = [
    "GeneralEventKind",
    "RedisPubsub",
    "channel_for",
    "get_pubsub",
    "publish",
    "publish_general",
    "publish_general_after_commit",
    "reset_pubsub",
    "shutdown",
    "subscribe",
    "subscribe_general",
    "subscriber_count",
]

from app.core.shutdown_registry import (
    register_web_shutdown_hook,
    register_worker_shutdown_hook,
)

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
