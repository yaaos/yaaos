"""structlog + OTel SDK initialization.

structlog is always initialized. The OTel SDK is **also** always initialized
— a `TracerProvider` is configured, the W3C trace-context propagator is set as
the global propagator, and FastAPI + SQLAlchemy auto-instrumentation runs. When
`OTEL_EXPORTER_OTLP_ENDPOINT` is set all three signal providers (traces, metrics,
logs) export to that endpoint via OTLP/HTTP; when unset (the default) providers
are created but exporters are not attached — signals are created and discarded.

This shape lets:
- wire-protocol code propagate `traceparent` headers across the agent boundary
  without runtime feature flags;
- structlog log records carry `trace_id` + `span_id` whenever a span is active;
- tests pull spans out of an in-memory exporter without ever wiring an OTLP endpoint.

Adding or changing the export destination is a single env-var flip
(`OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_EXPORTER_OTLP_HEADERS`).

Exporters are constructed with NO `endpoint=` / `headers=` kwargs so the SDK
reads `OTEL_EXPORTER_OTLP_ENDPOINT` (base URL) and appends `/v1/{traces,metrics,logs}`
per signal, and parses `OTEL_EXPORTER_OTLP_HEADERS` for auth. Passing `endpoint=`
explicitly skips the per-signal append, causing bare-base 404s.
"""

import logging
import sys
from typing import Any, Literal

import structlog

from app.core.config import get_settings

Role = Literal["app", "worker"]

_initialized = False

# Module-level provider references for shutdown — set once by _configure_otel.
_tracer_provider: Any = None
_meter_provider: Any = None
_logger_provider: Any = None


def configure(role: Role = "app") -> None:
    """Initialize structlog + OTel SDK. Idempotent — safe to call twice.

    `role` selects the OTel `service.name`: `app` (FastAPI process) or
    `worker` (taskiq consumer + outbox drain). The two processes deploy
    separately so they report under distinct service identities.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    settings = get_settings()
    service_name = settings.otel_service_name_worker if role == "worker" else settings.otel_service_name_app

    # ── OTel — three providers + W3C propagator, no exporters unless set ──
    # Initialize BEFORE structlog so the structlog processor below can
    # capture the active tracer/span.
    _configure_otel(
        endpoint=settings.otel_exporter_otlp_endpoint,
        service_name=service_name,
        service_version=settings.service_version,
        environment=settings.environment,
    )

    # ── structlog — routed through stdlib so app + library logs share one pipe
    # ─────────────────────────────────────────────────────────────────────────
    # Route structlog through stdlib `logging` so that OTel's `LoggingHandler`
    # (attached to the root logger below) captures both app logs (structlog) and
    # library logs (FastAPI / SQLAlchemy / uvicorn) in one OTLP stream.
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Renderer varies by mode: colored human-readable in non-prod, JSON in prod.
    if settings.is_non_prod:
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    # Shared pre-chain: runs before the final renderer on every record — both
    # structlog-native records AND foreign stdlib records (via foreign_pre_chain).
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_trace_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # stdlib StreamHandler → stdout (keeps JSON for Fly's log aggregator).
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    # Configure the root stdlib logger with our handler.  basicConfig is NOT
    # called here — we manage the root logger directly so we don't double-add
    # handlers on repeated configure() calls (idempotency is guarded above, but
    # defensive practice).
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(stream_handler)

    # Attach OTel's LoggingHandler at NOTSET so it inherits the root level —
    # one knob (LOG_LEVEL) gates both stdout and the OTLP export path.
    # Guard against double-attachment (e.g. if something called basicConfig first).
    if _logger_provider is not None:
        from opentelemetry.sdk._logs import LoggingHandler  # noqa: PLC0415

        otel_handler = LoggingHandler(logger_provider=_logger_provider)
        # NOTSET: inherit root level; no handler-level filter so the single
        # LOG_LEVEL knob controls both stdout and OTLP.
        otel_handler.setLevel(logging.NOTSET)
        root_logger.addHandler(otel_handler)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
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


def _configure_otel(
    *,
    endpoint: str | None,
    service_name: str,
    service_version: str,
    environment: str,
) -> None:
    """Always-on TracerProvider + MeterProvider + LoggerProvider + W3C propagator.

    Adds OTLP/HTTP exporters only when `endpoint` is set; otherwise all three
    providers are initialized without exporters (signals created and discarded).

    Exporters are constructed with NO `endpoint=` / `headers=` kwargs — the SDK
    reads `OTEL_EXPORTER_OTLP_ENDPOINT` (base) and appends `/v1/{signal}` per
    signal, and parses `OTEL_EXPORTER_OTLP_HEADERS` for auth. Passing `endpoint=`
    explicitly bypasses the per-signal path append and causes 404s.

    FastAPI + SQLAlchemy auto-instrumentation is wired here. Both libraries
    pick up the global tracer provider lazily; they emit spans whether or
    not an exporter is attached.
    """
    global _tracer_provider, _meter_provider, _logger_provider

    from opentelemetry import metrics, trace  # noqa: PLC0415
    from opentelemetry._logs import set_logger_provider  # noqa: PLC0415
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # noqa: PLC0415
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # noqa: PLC0415
    from opentelemetry.propagate import set_global_textmap  # noqa: PLC0415
    from opentelemetry.sdk._logs import LoggerProvider  # noqa: PLC0415
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor  # noqa: PLC0415
    from opentelemetry.sdk.metrics import MeterProvider  # noqa: PLC0415
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader  # noqa: PLC0415
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
    from opentelemetry.trace.propagation.tracecontext import (  # noqa: PLC0415
        TraceContextTextMapPropagator,
    )

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment.name": environment,
        }
    )

    # ── TracerProvider ─────────────────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        # No endpoint= / headers= kwargs — SDK reads OTEL_EXPORTER_OTLP_ENDPOINT
        # (base) and appends /v1/traces, and parses OTEL_EXPORTER_OTLP_HEADERS.
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(),
                max_export_batch_size=512,
                export_timeout_millis=30_000,
            )
        )
    trace.set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider

    # ── MeterProvider ──────────────────────────────────────────────────────
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: PLC0415
            OTLPMetricExporter,
        )

        # No endpoint= / headers= — SDK appends /v1/metrics to base endpoint.
        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(),
            export_interval_millis=60_000,
            export_timeout_millis=30_000,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    else:
        meter_provider = MeterProvider(resource=resource)
    metrics.set_meter_provider(meter_provider)
    _meter_provider = meter_provider

    # ── LoggerProvider ─────────────────────────────────────────────────────
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (  # noqa: PLC0415
            OTLPLogExporter,
        )

        # No endpoint= / headers= — SDK appends /v1/logs to base endpoint.
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(),
                max_export_batch_size=512,
                export_timeout_millis=30_000,
            )
        )
    else:
        logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)
    _logger_provider = logger_provider

    # ── Propagator ────────────────────────────────────────────────────────
    set_global_textmap(TraceContextTextMapPropagator())

    # ── Auto-instrumentation ──────────────────────────────────────────────
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


async def shutdown() -> None:
    """Force-flush and shut down all three OTel providers.

    Registered with both the web and worker shutdown registries so a rolling
    or bluegreen deploy flushes buffered telemetry rather than dropping the
    last batch. Called by the process lifespan teardown (web) or the worker
    drain orchestrator (worker).

    Generous timeouts (30 s per provider) are intentional — telemetry
    completeness wins over deploy speed. `fly.production.toml` `kill_timeout`
    must be set above the total flush budget.
    """
    global _tracer_provider, _meter_provider, _logger_provider

    _FLUSH_TIMEOUT_MS = 30_000

    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
        except Exception:
            pass
        try:
            _tracer_provider.shutdown()
        except Exception:
            pass

    if _meter_provider is not None:
        try:
            _meter_provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
        except Exception:
            pass
        try:
            _meter_provider.shutdown(timeout_millis=_FLUSH_TIMEOUT_MS)
        except Exception:
            pass

    if _logger_provider is not None:
        try:
            _logger_provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
        except Exception:
            pass
        try:
            _logger_provider.shutdown()
        except Exception:
            pass


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Bound structlog logger. Call configure() first (main.py does this)."""
    return structlog.get_logger(name) if name else structlog.get_logger()
