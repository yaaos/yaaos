"""structlog setup + conditional OTel SDK initialization.

structlog is always initialized. OTel is only initialized when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set; otherwise it is a silent no-op (no boot
failure, no exports). See architecture.md § Observability.
"""

import logging
import sys

import structlog

from app.core.config import get_settings

_initialized = False


def configure() -> None:
    """Initialize structlog + (optionally) OTel. Idempotent — safe to call twice."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    settings = get_settings()

    # ── structlog ───────────────────────────────────────────────────────
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, stream=sys.stdout, format="%(message)s")

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.yaaof_env == "dev":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ── OTel (conditional) ──────────────────────────────────────────────
    if settings.otel_enabled:
        _configure_otel(settings.otel_exporter_otlp_endpoint, settings.otel_service_name)


def _configure_otel(endpoint: str | None, service_name: str) -> None:
    """Wire up the OTel SDK with an OTLP exporter. Skipped if `endpoint` is falsy.

    Imports below are deliberately lazy — OTel is optional in M01 and should
    not be loaded at import time when disabled.
    """
    if not endpoint:
        return
    from opentelemetry import trace  # noqa: PLC0415 — lazy: only loaded when OTel enabled
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Bound structlog logger. Call configure() first (main.py does this)."""
    return structlog.get_logger(name) if name else structlog.get_logger()
