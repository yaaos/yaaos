"""core/sse — Redis-backed pub/sub for ActivityEvent fanout + general events.

Backed by Redis `PUBLISH`/`SUBSCRIBE` so a publish from the worker process
reaches an SSE subscriber attached to a different web process. Three channel
shapes:
- `activity:{workflow_execution_id}` — per-workflow activity stream.
- `{org_id}:general` — org-scoped general events with typed `GeneralEventKind`.
- `{org_id}:workspace_activity:{workflow_execution_id}` — per-org per-workflow activity events.
"""

from app.core.sse.service import (
    GeneralEventKind,
    RedisPubsub,
    channel_for,
    get_pubsub,
    publish,
    publish_general,
    publish_general_after_commit,
    publish_workspace_activity,
    register_workspace_activity_ownership_check,
    reset_pubsub,
    serialize_for_sse,
    shutdown,
    subscribe,
    subscribe_general,
    subscribe_workspace_activity,
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
    "publish_workspace_activity",
    "register_workspace_activity_ownership_check",
    "reset_pubsub",
    "serialize_for_sse",
    "shutdown",
    "subscribe",
    "subscribe_general",
    "subscribe_workspace_activity",
    "subscriber_count",
]

from app.core.shutdown_registry import (
    register_web_shutdown_hook,
    register_worker_shutdown_hook,
)

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
