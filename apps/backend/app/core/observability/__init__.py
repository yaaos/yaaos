"""core/observability — structlog + conditional OTel SDK + spawn helper.

M05 Phase 8 adds wire-protocol trace propagation helpers
(`current_traceparent`, `restore_traceparent_context`,
`with_remote_parent_span`) so spans nest across the task / wire-protocol
boundary.
"""

from app.core.observability.service import configure, get_logger
from app.core.observability.spawn import active_task_count, spawn
from app.core.observability.traceparent import (
    current_traceparent,
    restore_traceparent_context,
    with_remote_parent_span,
)

__all__ = [
    "active_task_count",
    "configure",
    "current_traceparent",
    "get_logger",
    "restore_traceparent_context",
    "spawn",
    "with_remote_parent_span",
]
