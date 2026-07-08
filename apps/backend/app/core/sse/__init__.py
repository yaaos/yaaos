"""core/sse — SSE semantics for general events and workspace-activity streams.

Channel naming and event shapes over the `core/redis` JSON pub/sub bus, so a
publish from the worker process reaches an SSE subscriber attached to a
different web process. Two channel shapes in active use:
- `{org_id}:general` — org-scoped general events with typed `GeneralEventKind`.
- `{org_id}:workspace_activity:{run_id}` — per-org per-run activity events.

`shutdown()` sets the process-wide close event so all active stream generators
emit a final `retry:`+comment frame and return; registered with the web
shutdown registry (SSE is web-presence only).
"""

from app.core.shutdown_registry import register_web_shutdown_hook
from app.core.sse.service import (
    GeneralEventKind,
    publish_general,
    publish_general_after_commit,
    publish_workspace_activity,
    serialize_for_sse,
    sse_prelude,
    subscribe_general,
    subscribe_workspace_activity,
)
from app.core.sse.web import set_shutdown_event_for_tests, shutdown

__all__ = [
    "GeneralEventKind",
    "publish_general",
    "publish_general_after_commit",
    "publish_workspace_activity",
    "serialize_for_sse",
    "set_shutdown_event_for_tests",
    "shutdown",
    "sse_prelude",
    "subscribe_general",
    "subscribe_workspace_activity",
]

register_web_shutdown_hook(shutdown)
