"""core/observability — structlog + conditional OTel SDK + spawn helper.

adds wire-protocol trace propagation helpers
(`current_traceparent`, `restore_traceparent_context`,
`with_remote_parent_span`) so spans nest across the task / wire-protocol
boundary.
"""

from app.core.observability.service import (
    TRACE_EXCLUDED_URLS,
    YaaosDimensionsSpanProcessor,
    configure,
    get_logger,
    shutdown,
)
from app.core.observability.slow_request import (
    SLOW_REQUEST_THRESHOLD_MS,
    SlowRequestLogMiddleware,
)
from app.core.observability.spawn import active_task_count, spawn
from app.core.observability.traceparent import (
    current_traceparent,
    restore_traceparent_context,
    with_remote_parent_span,
)
from app.core.shutdown_registry import (
    register_web_shutdown_hook,
    register_worker_shutdown_hook,
)

__all__ = [
    "SLOW_REQUEST_THRESHOLD_MS",
    "TRACE_EXCLUDED_URLS",
    "SlowRequestLogMiddleware",
    "YaaosDimensionsSpanProcessor",
    "active_task_count",
    "configure",
    "current_traceparent",
    "get_logger",
    "restore_traceparent_context",
    "shutdown",
    "spawn",
    "with_remote_parent_span",
]

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
