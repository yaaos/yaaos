"""structlog + OTel SDK initialization.

structlog is always initialized. The OTel SDK is **also** always initialized
as of M05 Phase 0c — a `TracerProvider` is configured, the W3C trace-context
propagator is set as the global propagator, and FastAPI + SQLAlchemy auto-
instrumentation runs. When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, spans flow
to that endpoint; when unset (the default today) spans are created and
discarded.

This shape lets:
- M05's wire-protocol code propagate `traceparent` headers across the
  agent boundary without runtime feature flags;
- structlog log records carry `trace_id` + `span_id` whenever a span is
  active;
- tests pull spans out of an in-memory exporter without ever wiring an
  OTLP endpoint.

Adding a real exporter in prod later (Datadog / Honeycomb / Tempo) is a
single env-var flip.
"""

import logging
import sys
from typing import Any

import structlog

from app.core.config import get_settings

_initialized = False


def configure() -> None:
    """Initialize structlog + OTel SDK. Idempotent — safe to call twice."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    settings = get_settings()

    # ── OTel — TracerProvider + W3C propagator, no exporter unless set ──
    # Initialize BEFORE structlog so the structlog processor below can
    # capture the active tracer/span.
    _configure_otel(
        endpoint=settings.otel_exporter_otlp_endpoint,
        service_name=settings.otel_service_name,
    )

    # ── structlog ───────────────────────────────────────────────────────
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, stream=sys.stdout, format="%(message)s")

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_trace_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.is_non_prod:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _inject_trace_context(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor — annotate each log record with `trace_id` +
    `span_id` from the currently active OTel span. No-op when no span is
    active (e.g. boot-time logs before the first request)."""
    from opentelemetry import trace  # noqa: PLC0415

    span = trace.get_current_span()
    if span is trace.INVALID_SPAN:
        return event_dict
    ctx = span.get_span_context()
    if ctx is None or not ctx.is_valid:
        return event_dict
    event_dict["trace_id"] = format(ctx.trace_id, "032x")
    event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _configure_otel(*, endpoint: str | None, service_name: str) -> None:
    """Always-on TracerProvider + W3C propagator. Adds an OTLP exporter only
    when `endpoint` is set; otherwise spans are created and discarded.

    FastAPI + SQLAlchemy auto-instrumentation is wired here. Both libraries
    pick up the global tracer provider lazily; they emit spans whether or
    not an exporter is attached.
    """
    from opentelemetry import trace  # noqa: PLC0415
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415
    from opentelemetry.propagate import set_global_textmap  # noqa: PLC0415
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.trace.propagation.tracecontext import (  # noqa: PLC0415
        TraceContextTextMapPropagator,
    )

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

    trace.set_tracer_provider(provider)
    set_global_textmap(TraceContextTextMapPropagator())

    # Auto-instrumentation. Idempotent: both libraries no-op on second
    # invocation, which matters for tests that reload the module.
    # Idempotent: already-instrumented raises on a second call, which only
    # matters for tests that reload the module. Swallow that case explicitly.
    try:
        FastAPIInstrumentor().instrument()
    except Exception:
        pass
    try:
        SQLAlchemyInstrumentor().instrument()
    except Exception:
        pass


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Bound structlog logger. Call configure() first (main.py does this)."""
    return structlog.get_logger(name) if name else structlog.get_logger()
