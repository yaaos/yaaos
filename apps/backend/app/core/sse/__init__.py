"""core/sse — SSE semantics for general events and workspace-activity streams.

Channel naming and event shapes over the `core/redis` JSON pub/sub bus, so a
publish from the worker process reaches an SSE subscriber attached to a
different web process. Two channel shapes in active use:
- `{org_id}:general` — org-scoped general events with typed `GeneralEventKind`.
- `{org_id}:workspace_activity:{workflow_execution_id}` — per-org per-workflow activity events.
"""

from app.core.sse.service import (
    GeneralEventKind,
    publish_general,
    publish_general_after_commit,
    publish_workspace_activity,
    serialize_for_sse,
    subscribe_general,
    subscribe_workspace_activity,
)

__all__ = [
    "GeneralEventKind",
    "publish_general",
    "publish_general_after_commit",
    "publish_workspace_activity",
    "serialize_for_sse",
    "subscribe_general",
    "subscribe_workspace_activity",
]
