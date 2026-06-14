"""Helpers for serializing OTel trace context to a `traceparent` string and
restoring it when a downstream worker picks up the work.

The wire protocol carries W3C `traceparent` strings on every AgentCommand,
AgentEvent, and `core/tasks` task argument set. These helpers are the
seam between the in-process OTel SDK (set up in `core/observability`)
and that wire format.

Usage:

    # Side A: about to enqueue a downstream task.
    args = {"workflow_execution_id": ..., "traceparent": current_traceparent()}
    await enqueue(START_STEP, args=args, session=s)

    # Side B: inside the task body.
    tracer = tracer_for("core.workflow")
    with start_child_span_with_traceparent(tracer, "start_step", traceparent):
        ...

Both helpers no-op gracefully when no OTel SDK is configured (current span
is INVALID_SPAN) — emitting spans is always optional and never blocks the
business logic.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.context import Context, attach, detach
from opentelemetry.trace import SpanKind, TraceFlags
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

_PROPAGATOR = TraceContextTextMapPropagator()


def current_traceparent() -> str | None:
    """Serialize the currently active span context as a `traceparent`
    string. Returns None when no span is active (boot, tests without an
    explicit span).
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    # We construct the traceparent manually for stability — the propagator
    # writes into a carrier dict, and we want just the header value.
    flags = "01" if ctx.trace_flags & TraceFlags.SAMPLED else "00"
    return f"00-{ctx.trace_id:032x}-{ctx.span_id:016x}-{flags}"


def restore_traceparent_context(traceparent: str | None) -> Context | None:
    """Parse `traceparent` and return an OTel `Context` whose active span
    points at the encoded remote span. Returns None when `traceparent`
    is empty or malformed.

    Used by task-body wrappers to make `start_as_current_span(...)` nest
    children under the upstream span across the worker boundary.
    """
    if not traceparent:
        return None
    carrier = {"traceparent": traceparent}
    ctx = _PROPAGATOR.extract(carrier)
    # The propagator returns the input context unchanged when extraction
    # fails. Detect that by checking whether a valid span context now
    # lives inside.
    span = trace.get_current_span(ctx)
    if not span.get_span_context().is_valid:
        return None
    return ctx


@contextmanager
def with_remote_parent_span(
    tracer: trace.Tracer,
    name: str,
    traceparent: str | None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Iterator[trace.Span]:
    """Open a span whose parent is the span encoded in `traceparent`. When
    `traceparent` is None / malformed, the span starts a fresh trace —
    the caller's code path is unchanged.

    Pairs with `current_traceparent()` on the producer side to weave a
    single trace across the worker boundary.
    """
    ctx = restore_traceparent_context(traceparent)
    if ctx is None:
        with tracer.start_as_current_span(name, kind=kind) as span:
            yield span
        return
    token = attach(ctx)
    try:
        with tracer.start_as_current_span(name, kind=kind) as span:
            yield span
    finally:
        detach(token)


__all__ = [
    "current_traceparent",
    "restore_traceparent_context",
    "with_remote_parent_span",
]
