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
import re
import sys
from contextlib import contextmanager
from typing import Any, Literal

import structlog

from app.core.config import get_settings

Role = Literal["app", "worker"]

# Comma-delimited regexes (OTel `excluded_urls` syntax) of paths the FastAPI
# instrumentor must NOT trace. `/api/health` is hit constantly by Fly's machine
# checker; tracing each probe is pure noise. Matched with re.search against the
# request path, so the bare substring covers the leading-slash form.
TRACE_EXCLUDED_URLS = "api/health"

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
        _redact_secrets,
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
    # Handler-level gating at log_level (not NOTSET): the access-log demotion
    # below relies on handlers dropping DEBUG records that propagate up from
    # `uvicorn.access` — logger-level gating alone would not, since a
    # propagated record is not re-checked against the root logger's level.
    stream_handler.setLevel(log_level)

    # Configure the root stdlib logger with our handler.  basicConfig is NOT
    # called here — we manage the root logger directly so we don't double-add
    # handlers on repeated configure() calls (idempotency is guarded above, but
    # defensive practice).
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    # Clear any previously added filters (idempotency guard).
    root_logger.filters.clear()
    # Stamp yaaos dimension contextvars as LogRecord attributes so the OTel
    # LoggingHandler maps them to queryable log attributes in Dash0.
    root_logger.addFilter(_YaaosLogDimsFilter())
    # Mask secret-keyed values on foreign records before any handler — covers
    # the OTLP export pipe, which the structlog foreign_pre_chain does not reach.
    root_logger.addFilter(_SecretScrubFilter())
    root_logger.addHandler(stream_handler)

    # Demote uvicorn access logs to DEBUG so production (LOG_LEVEL=INFO) drops
    # them from both stdout and OTLP while access_log stays enabled. Survives
    # because app/web.py passes log_config=None to uvicorn (no dict-config to
    # clobber it) and uvicorn.access then propagates to this root logger.
    logging.getLogger("uvicorn.access").addFilter(_AccessLogDebugFilter())

    # Attach OTel's LoggingHandler gated at log_level so the single LOG_LEVEL
    # knob controls both stdout and the OTLP export path (and so demoted
    # access-log DEBUG records are dropped from OTLP in production).
    # Guard against double-attachment (e.g. if something called basicConfig first).
    if _logger_provider is not None:
        from opentelemetry.sdk._logs import LoggingHandler  # noqa: PLC0415

        otel_handler = LoggingHandler(logger_provider=_logger_provider)
        otel_handler.setLevel(log_level)
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


# ── Secret redaction ─────────────────────────────────────────────────────────

# Keys whose values are secret-ish and must never be rendered raw. Match is
# case-insensitive substring. `bearer` catches BearerContext field names;
# `authorization` covers HTTP header dicts; `token` covers token_hash and
# friends; `signed_request` carries AWS sigv4 secrets.
_REDACT_KEY_RE = re.compile(r"(?i)(authorization|bearer|token|secret|password|api[_-]?key|signed_request)")
_REDACT_MASK = "***"


def _scrub(value: Any) -> Any:
    """Recursively mask values under any secret-matching key.

    Dicts: mask matching keys, recurse into the rest. Lists/tuples: recurse
    per-element (preserving type) so a list of header dicts is scrubbed too.
    Scalars pass through. Key-based by design — free-text strings are left
    intact (parsing arbitrary text for secrets is unreliable and mangles
    legitimate logs)."""
    if isinstance(value, dict):
        return {
            k: (_REDACT_MASK if isinstance(k, str) and _REDACT_KEY_RE.search(k) else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v) for v in value)
    return value


def _redact_secrets(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor — mask secret-keyed values in the event dict.

    Runs in `shared_processors`, so it covers both structlog-native records and
    foreign stdlib records routed through the `ProcessorFormatter`'s
    `foreign_pre_chain` on the stdout pipe. The OTLP export pipe is covered
    separately by `_SecretScrubFilter` on the root logger, because the OTel
    `LoggingHandler` bypasses the `ProcessorFormatter`."""
    return _scrub(event_dict)  # type: ignore[no-any-return]


# ── Standard dimension helpers ──────────────────────────────────────────────

# Attribute names emitted on every span + log record when contextvars are set.
_ATTR_ORG_ID = "yaaos.org_id"
_ATTR_USER_ID = "yaaos.user_id"
_ATTR_ACTOR_KIND = "yaaos.actor_kind"
_ATTR_WORKFLOW_ID = "yaaos.workflow_id"
_ATTR_COMMAND_ID = "yaaos.command_id"

# LogRecord attribute names (snake_case, no dot — LogRecord attrs can't have
# dots; the LoggingHandler picks up all non-reserved attrs from vars(record)).
_LOG_ATTR_ORG_ID = "yaaos_org_id"
_LOG_ATTR_USER_ID = "yaaos_user_id"
_LOG_ATTR_ACTOR_KIND = "yaaos_actor_kind"
_LOG_ATTR_WORKFLOW_ID = "yaaos_workflow_execution_id"
_LOG_ATTR_COMMAND_ID = "yaaos_command_id"


class YaaosDimensionsSpanProcessor:
    """OTel `SpanProcessor` that stamps standard yaaos dimensions on every span
    at creation (`on_start`).

    Reads the auth contextvars (`org_id_var`, `user_id_var`, `actor_kind_var`,
    `workflow_execution_id_var`, `command_id_var`) and sets the corresponding
    attributes on the span. Attributes are only stamped when the var is set
    (not None/empty) — background spans carry org+actor but no `user_id`;
    non-workflow spans carry no `workflow_id`/`command_id`.

    OTel attributes do NOT inherit to child spans, so a processor on `on_start`
    is the only mechanism that makes dims universal without per-span code.
    """

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        # Late import to avoid circular-import at module level (auth → context;
        # observability must not import auth at module level).
        from app.core.auth import (  # noqa: PLC0415
            actor_kind_var,
            command_id_var,
            org_id_var,
            user_id_var,
            workflow_execution_id_var,
        )

        org_id = org_id_var.get()
        user_id = user_id_var.get()
        actor_kind = actor_kind_var.get()
        workflow_id = workflow_execution_id_var.get()
        command_id = command_id_var.get()

        if org_id is not None:
            span.set_attribute(_ATTR_ORG_ID, str(org_id))
        if user_id is not None:
            span.set_attribute(_ATTR_USER_ID, str(user_id))
        if actor_kind is not None:
            span.set_attribute(_ATTR_ACTOR_KIND, actor_kind.value)
        if workflow_id is not None:
            span.set_attribute(_ATTR_WORKFLOW_ID, workflow_id)
        if command_id is not None:
            span.set_attribute(_ATTR_COMMAND_ID, command_id)

    def on_end(self, span: Any) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


class _YaaosLogDimsFilter(logging.Filter):
    """stdlib logging Filter that stamps yaaos dimension contextvars as
    `LogRecord` attributes before the OTel `LoggingHandler` processes the
    record.

    OTel's `LoggingHandler._get_attributes` picks up every non-reserved
    attribute from `vars(record)`, so setting `record.yaaos_org_id = "..."` etc.
    makes the dims queryable in Dash0 as log attributes — not just embedded in
    the formatted message string.

    Runs on every record that passes through the root logger, so both
    structlog-native records and foreign library records (FastAPI/SQLAlchemy)
    carry the same dims.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from app.core.auth import (  # noqa: PLC0415
            actor_kind_var,
            command_id_var,
            org_id_var,
            user_id_var,
            workflow_execution_id_var,
        )

        org_id = org_id_var.get()
        user_id = user_id_var.get()
        actor_kind = actor_kind_var.get()
        workflow_id = workflow_execution_id_var.get()
        command_id = command_id_var.get()

        if org_id is not None:
            setattr(record, _LOG_ATTR_ORG_ID, str(org_id))
        if user_id is not None:
            setattr(record, _LOG_ATTR_USER_ID, str(user_id))
        if actor_kind is not None:
            setattr(record, _LOG_ATTR_ACTOR_KIND, actor_kind.value)
        if workflow_id is not None:
            setattr(record, _LOG_ATTR_WORKFLOW_ID, workflow_id)
        if command_id is not None:
            setattr(record, _LOG_ATTR_COMMAND_ID, command_id)

        return True  # never suppress; only annotate


class _SecretScrubFilter(logging.Filter):
    """stdlib Filter that masks secret-keyed values on foreign log records
    before any handler sees them — crucially the OTel `LoggingHandler`, which
    is attached directly to the root logger and so bypasses structlog's
    `ProcessorFormatter` (where `foreign_pre_chain` runs). Without this filter,
    foreign library records (FastAPI / SQLAlchemy / httpx / uvicorn) would
    export to OTLP unscrubbed.

    Scrubs structured payloads only: a dict `record.msg` and dict / tuple
    `record.args` (the values a library interpolates into its message — e.g. a
    header dict in an httpx error). Free-text `record.msg` is left intact for
    the same reason `_scrub` is key-based: pattern-masking arbitrary text is
    unreliable and mangles legitimate logs. App records are already
    `SecretStr`-masked at the boundary, so this targets the foreign-record gap."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, dict):
            record.msg = _scrub(record.msg)
        if isinstance(record.args, dict):
            record.args = _scrub(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(_scrub(a) for a in record.args)
        return True  # never suppress; only scrub


class _AccessLogDebugFilter(logging.Filter):
    """stdlib Filter that demotes `uvicorn.access` records to DEBUG severity.

    uvicorn emits one access line per request at INFO. Production runs at
    `LOG_LEVEL=INFO`, so demoting to DEBUG drops access lines from both the
    stdout handler and the OTLP export path (both gated at `log_level`) while
    keeping `access_log=True` — they resurface only when an operator runs at
    `LOG_LEVEL=DEBUG`. Installed on the `uvicorn.access` logger in `configure()`;
    `app/web.py` passes `log_config=None` to uvicorn so this filter survives
    (uvicorn's default dict-config would otherwise reconfigure the logger)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True


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
    # Stamp standard dims on every span at creation so child spans always
    # carry org_id/user_id/actor_kind/workflow_id/command_id without per-span
    # set_attribute calls.  SynchronousMultiSpanProcessor runs on_start in the
    # calling thread — reading contextvars inside on_start is safe.
    tracer_provider.add_span_processor(YaaosDimensionsSpanProcessor())
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
        FastAPIInstrumentor().instrument(excluded_urls=TRACE_EXCLUDED_URLS)
    except Exception:
        pass
    try:
        SQLAlchemyInstrumentor().instrument()
    except Exception:
        pass
    # System metrics (CPU / mem / GC) for both web and worker processes.
    # SystemMetricsInstrumentor reads from psutil and registers observable
    # instruments against the supplied meter_provider; no global state mutated.
    # Idempotent guard matches the pattern above — a second call raises if
    # already instrumented; swallow to keep configure() safe in test re-runs.
    try:
        from opentelemetry.instrumentation.system_metrics import (  # noqa: PLC0415
            SystemMetricsInstrumentor,
        )

        SystemMetricsInstrumentor().instrument(meter_provider=meter_provider)
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


@contextmanager
def _scoped_otel_providers(
    *,
    tracer: Any = None,
    meter: Any = None,
    logger: Any = None,
):
    """Test seam: temporarily point the module-level provider refs that
    `shutdown()` flushes at the supplied providers, restoring the PRIOR refs
    (not None) on exit. Intra-module only — not in `__all__`; reached via a
    direct submodule import from `observability/test/`."""
    global _tracer_provider, _meter_provider, _logger_provider
    prior = (_tracer_provider, _meter_provider, _logger_provider)
    if tracer is not None:
        _tracer_provider = tracer
    if meter is not None:
        _meter_provider = meter
    if logger is not None:
        _logger_provider = logger
    try:
        yield
    finally:
        _tracer_provider, _meter_provider, _logger_provider = prior


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Bound structlog logger. Call configure() first (main.py does this)."""
    return structlog.get_logger(name) if name else structlog.get_logger()
