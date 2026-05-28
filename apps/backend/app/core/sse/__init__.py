"""core/sse — Redis-backed pub/sub for general events and workspace-activity streams.

Backed by Redis `PUBLISH`/`SUBSCRIBE` so a publish from the worker process
reaches an SSE subscriber attached to a different web process. Two channel
shapes in active use:
- `{org_id}:general` — org-scoped general events with typed `GeneralEventKind`.
- `{org_id}:workspace_activity:{workflow_execution_id}` — per-org per-workflow activity events.
"""

from app.core.sse.service import (
    GeneralEventKind,
    RedisPubsub,
    get_pubsub,
    publish_general,
    publish_general_after_commit,
    publish_workspace_activity,
    register_workspace_activity_ownership_check,
    reset_pubsub,
    reset_workspace_activity_ownership_check,
    serialize_for_sse,
    shutdown,
    subscribe_general,
    subscribe_workspace_activity,
    subscriber_count,
)

__all__ = [
    "GeneralEventKind",
    "RedisPubsub",
    "get_pubsub",
    "publish_general",
    "publish_general_after_commit",
    "publish_workspace_activity",
    "register_workspace_activity_ownership_check",
    "reset_pubsub",
    "reset_workspace_activity_ownership_check",
    "serialize_for_sse",
    "shutdown",
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
