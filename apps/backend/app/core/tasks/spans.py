"""Worker task span middleware — records task execution as an OTel span.

`TaskSpanMiddleware` wraps each task body in a span named `task:<task_name>`.
On exception it calls `span.record_exception(exc)` + `span.set_status(ERROR)`
so background/worker errors are visible on traces — not just in logs.

The middleware uses the same `pre_execute` / `post_execute` / `on_error`
lifecycle as `TaskMetricsMiddleware` (see `metrics.py`).

`TaskSpanMiddleware` is a taskiq `TaskiqMiddleware` wired into the broker by
`runtime.run()` alongside `OrgContextMiddleware` and `TaskMetricsMiddleware`.

Test isolation: `TaskSpanMiddleware` accepts an optional `tracer=` argument so
tests can inject a tracer from a local `TracerProvider` backed by an
`InMemorySpanExporter` without touching the global OTel provider state.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import StatusCode, Tracer
from taskiq import TaskiqMessage, TaskiqMiddleware
from taskiq.result import TaskiqResult

_tracer = trace.get_tracer(__name__)


class TaskSpanMiddleware(TaskiqMiddleware):
    """Wraps each task body in an OTel span; records exceptions as span events.

    Span name: `task:<task_name>`.

    On exception:
      - `span.record_exception(exc)` — attaches exception type + message as a
        span event so the trace carries the failure detail.
      - `span.set_status(ERROR)` — marks the span (and its enclosing trace) as
        failed so error-biased samplers and dashboards surface it.

    By default uses the module-level proxy tracer (global OTel provider). Pass an
    explicit `tracer=` to inject a test-local tracer backed by an in-memory exporter.
    """

    def __init__(self, *, tracer: Tracer | None = None) -> None:
        super().__init__()
        self._tracer: Tracer = tracer if tracer is not None else _tracer
        # Per-task-invocation open spans keyed on taskiq task_id (per-invocation
        # UUID). Each value is the (span, context-token) pair: the token is
        # returned by `context.attach` and must be passed to `context.detach`
        # when the body finishes so the prior context is restored.
        # asyncio is cooperative and each task_id is unique so no lock is needed.
        self._spans: dict[str, tuple[Any, object]] = {}

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        # `start_span` only creates the span — it does NOT install it as the
        # current context. Attach it so spans created inside the task body
        # (SQLAlchemy auto-instrumentation, manual spans) nest under it rather
        # than under whatever context was active at dequeue time.
        span = self._tracer.start_span(f"task:{message.task_name}")
        token = otel_context.attach(trace.set_span_in_context(span))
        self._spans[message.task_id] = (span, token)
        return message

    async def post_execute(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
    ) -> None:
        entry = self._spans.pop(message.task_id, None)
        if entry is None:
            return
        span, token = entry
        try:
            if result.is_err and result.error is not None:
                exc = result.error
                if isinstance(exc, Exception):
                    span.record_exception(exc)
                    span.set_status(StatusCode.ERROR, str(exc))
        finally:
            otel_context.detach(token)
            span.end()

    async def on_error(
        self,
        message: TaskiqMessage,
        result: TaskiqResult[Any],
        exception: BaseException,
    ) -> None:
        entry = self._spans.pop(message.task_id, None)
        if entry is None:
            return
        span, token = entry
        try:
            if isinstance(exception, Exception):
                span.record_exception(exception)
                span.set_status(StatusCode.ERROR, str(exception))
        finally:
            otel_context.detach(token)
            span.end()


# Module-level singleton wired into the broker at worker boot (see runtime.py).
task_span_middleware = TaskSpanMiddleware()
