# core/observability

> structlog + three-signal OTel SDK initialization, spawn helper, and wire-protocol trace helpers.

## Scope

- Owns: structlog config, OTel `TracerProvider` + `MeterProvider` + `LoggerProvider` + propagator + auto-instrumentation, `spawn()`, `SlowRequestLogMiddleware`, wire-protocol trace helpers, `shutdown()` flush.
- No HTTP routes, no tables.

## Why / invariants

**OTel always on, exporters optional** — all three providers are always initialized. OTLP/HTTP exporters are attached only when `otel_exporter_otlp_endpoint` is set. Adding a real exporter in prod is a single env-var flip (`OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_EXPORTER_OTLP_HEADERS`); no code change or feature flags.

**Metric sources** — the MeterProvider is non-empty from boot:
- `FastAPIInstrumentor` emits `http.server.duration`, `http.server.active_requests`, and `http.server.response.size` automatically once the global MeterProvider is set (web process only).
- `SystemMetricsInstrumentor` (from `opentelemetry-instrumentation-system-metrics`, which pulls `psutil`) registers CPU, memory, and GC observable instruments on both the web and worker processes. Wired in `_configure_otel` by passing `meter_provider=` explicitly — no global state mutation beyond what the SDK owns.

**Health probes are not traced** — `TRACE_EXCLUDED_URLS` (`"api/health"`) is passed to the FastAPI instrumentor (both the global `instrument()` in `_configure_otel` and the fallback `instrument_app()` in `core/webserver`), so `/api/health` produces no HTTP server span. The health DB ping (`core/database.ping()`, used by web `/api/health` and worker `/health`) runs inside `suppress_instrumentation()` so the constant probes emit no SQLAlchemy span either. Fly's machine checker hits health every few seconds; tracing each probe would be pure noise.

**Exporter no-arg construction** — exporters are constructed with NO `endpoint=` / `headers=` kwargs. The SDK reads `OTEL_EXPORTER_OTLP_ENDPOINT` (base URL, e.g. `https://ingress.<region>.aws.dash0.com`) and appends `/v1/{traces,metrics,logs}` per signal; `OTEL_EXPORTER_OTLP_HEADERS` carries the `Authorization: Bearer …,Dash0-Dataset: …` pair. Passing `endpoint=` explicitly skips the per-signal append → bare-base 404, telemetry silently dropped.

**`configure(role=...)` must be called once at boot** — `"app"` from `web.py`, `"worker"` from `core/tasks/runtime.py`. Sets `service.name` accordingly. Idempotent (module-level `_initialized` flag + OTel "already instrumented" guard).

**Resource attributes** — every provider's resource carries `service.name`, `service.version` (from `settings.service_version`), and `deployment.environment.name` (from `settings.environment`).

**structlog routed through stdlib** — structlog uses `stdlib.LoggerFactory()` + a `ProcessorFormatter` on a stdlib `StreamHandler`. OTel's `LoggingHandler` is attached to the **root** stdlib logger. Both handlers are gated at `log_level` (not `NOTSET`): handler-level gating is required so that records demoted to DEBUG after propagating up — e.g. `uvicorn.access` (see below) — are dropped, since a propagated record is not re-checked against the root logger's level. Result: app logs (structlog) and library logs (FastAPI / SQLAlchemy / uvicorn) share one pipe and both reach Dash0; `LOG_LEVEL` gates both stdout and OTLP export. `web.py` passes `log_config=None` to uvicorn so uvicorn's loggers propagate to root instead of running their own dict-config.

**Secret redaction — two pipes, two gates.** `SecretStr` self-masks app-emitted secrets at every boundary, but foreign library records (uvicorn / SQLAlchemy / httpx) don't go through `SecretStr`, so two key-based scrubbers (`_REDACT_KEY_RE`: `authorization|bearer|token|secret|password|api_key|signed_request`, mask `***`) cover them:
- `_redact_secrets` — structlog processor in the shared chain; runs in the `ProcessorFormatter`'s `foreign_pre_chain`, so it scrubs the **stdout** pipe for both app and foreign records.
- `_SecretScrubFilter` — stdlib `logging.Filter` on the root logger; scrubs the **OTLP** pipe, which the structlog `ProcessorFormatter` never reaches because the OTel `LoggingHandler` is attached directly to root. Scrubs structured payloads only (dict `record.msg`, dict / tuple `record.args`); free-text messages pass through (key-based masking can't parse arbitrary text).

**`uvicorn.access` demoted to DEBUG** — `_AccessLogDebugFilter` on the `uvicorn.access` logger rewrites each access record to DEBUG severity. Production (`LOG_LEVEL=INFO`) drops them from both stdout and OTLP while `access_log` stays enabled; they resurface only at `LOG_LEVEL=DEBUG`. Depends on `log_config=None` (above) so uvicorn doesn't reconfigure the logger and discard the filter.

**structlog processor chain** (shared across structlog-native and stdlib foreign records):
`merge_contextvars` → `_inject_trace_context` (pulls `trace_id`/`span_id` from active OTel span) → `_redact_secrets` → `add_log_level` → ISO-UTC timestamp → stack/exception formatters → `ConsoleRenderer` (non-prod) or `JSONRenderer` (prod). Per-mode renderer lives inside `ProcessorFormatter`.

**`shutdown()` registered on both registries** — registered at import time with `register_web_shutdown_hook` AND `register_worker_shutdown_hook` so every deploy path (web lifespan teardown + worker SIGTERM drain) force-flushes all three providers before the process exits. 30 s flush timeout per provider; `fly.production.toml` `kill_timeout` must exceed the total flush budget. None of this existed before — a running deploy previously dropped the last buffered batch.

**Wire-protocol trace propagation:**
- `current_traceparent()` — serializes active span context as W3C `traceparent`.
- `restore_traceparent_context(traceparent)` — parses back to OTel `Context`.
- `with_remote_parent_span(tracer, name, traceparent)` — emits a span under the remote trace; falls back to fresh trace on None/malformed.

`domain/intake/web.post_intake` records `current_traceparent()` when a webhook arrives and passes it into `core/workflow`; downstream tasks restore it. This is what gives one `trace_id` across webhook → terminal outcome.

**Standard dims on every span + log** — `YaaosDimensionsSpanProcessor` is registered on the `TracerProvider` during `_configure_otel`. Its `on_start` reads the auth contextvars (`org_id_var`, `user_id_var`, `actor_kind_var`, `workflow_execution_id_var`, `command_id_var` from `core/auth/context`) and stamps the non-None values as `yaaos.org_id`, `yaaos.user_id`, `yaaos.actor_kind`, `yaaos.workflow_id`, `yaaos.command_id` on every new span. OTel attributes do not inherit to child spans — `on_start` is the only mechanism that makes dims universal without per-span code. The `_YaaosLogDimsFilter` (stdlib `logging.Filter` added to the root logger by `configure()`) does the same for log records: it sets matching `LogRecord` attributes (`yaaos_org_id`, `yaaos_user_id`, etc.) from contextvars, which the OTel `LoggingHandler._get_attributes` then maps to queryable log attributes in Dash0. Both filters only stamp when the var is set — background spans carry org+actor but no `user_id`; non-workflow spans carry no `workflow_id`/`command_id`.

**`spawn(name, coro)`** — `asyncio.create_task` wrapped with an OTel span (`spawn:{name}`) + error recording. On exception: `span.record_exception(exc)` + `span.set_status(ERROR)` before the `spawn.crashed` log line. Does not re-raise. Task retained in a module-level set to prevent GC mid-flight.

**`SlowRequestLogMiddleware`** — emits `http.slow_request` warn for requests ≥ `SLOW_REQUEST_THRESHOLD_MS` (default 500ms). Never throws.

## Public interface

- `configure(role)` — initialize structlog + all three OTel providers. Call once at boot.
- `shutdown()` — async; force-flush + shut down all three providers. Registered with both shutdown registries automatically at import.
- `get_logger(name?)` — bound structlog logger.
- `spawn(name, coro, *, tracer?)` — fire-and-forget background task: OTel span + exception recording + error log. `tracer=` injection point for tests.
- `current_traceparent()`, `restore_traceparent_context(tp)`, `with_remote_parent_span(tracer, name, tp)` — wire-protocol trace helpers.
- `SlowRequestLogMiddleware`, `SLOW_REQUEST_THRESHOLD_MS` — slow-request logging middleware.
- `active_task_count()` — number of in-flight spawned tasks (test helper).
- `YaaosDimensionsSpanProcessor` — `SpanProcessor` that stamps standard yaaos dims on every span at creation (see § Standard dims below).

## Entry points

- `apps/backend/app/core/observability/service.py` — configure + shutdown implementation.
- `apps/backend/app/core/observability/__init__.py` — public re-exports + shutdown hook registration.
- `apps/backend/app/core/observability/spawn.py` — spawn helper.
- `apps/backend/app/core/observability/traceparent.py` — wire-protocol helpers.
- `apps/backend/app/core/observability/slow_request.py` — slow-request middleware.
