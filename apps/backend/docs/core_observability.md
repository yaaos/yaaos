# core/observability

> structlog + three-signal OTel SDK initialization, spawn helper, and wire-protocol trace helpers.

## Scope

- Owns: structlog config, OTel `TracerProvider` + `MeterProvider` + `LoggerProvider` + propagator + auto-instrumentation, `spawn()`, `SlowRequestLogMiddleware`, wire-protocol trace helpers, `shutdown()` flush.
- No HTTP routes, no tables.

## Why / invariants

**OTel always on, exporters optional** — all three providers are always initialized. OTLP/HTTP exporters are attached only when `otel_exporter_otlp_endpoint` is set. Adding a real exporter in prod is a single env-var flip (`OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_EXPORTER_OTLP_HEADERS`); no code change or feature flags.

**Exporter no-arg construction** — exporters are constructed with NO `endpoint=` / `headers=` kwargs. The SDK reads `OTEL_EXPORTER_OTLP_ENDPOINT` (base URL, e.g. `https://ingress.<region>.aws.dash0.com`) and appends `/v1/{traces,metrics,logs}` per signal; `OTEL_EXPORTER_OTLP_HEADERS` carries the `Authorization: Bearer …,Dash0-Dataset: …` pair. Passing `endpoint=` explicitly skips the per-signal append → bare-base 404, telemetry silently dropped.

**`configure(role=...)` must be called once at boot** — `"app"` from `web.py`, `"worker"` from `core/tasks/runtime.py`. Sets `service.name` accordingly. Idempotent (module-level `_initialized` flag + OTel "already instrumented" guard).

**Resource attributes** — every provider's resource carries `service.name`, `service.version` (from `settings.service_version`), and `deployment.environment.name` (from `settings.environment`).

**structlog routed through stdlib** — structlog uses `stdlib.LoggerFactory()` + a `ProcessorFormatter` on a stdlib `StreamHandler`. OTel's `LoggingHandler` is attached to the **root** stdlib logger at `NOTSET` so it inherits the root `log_level` filter. Result: app logs (structlog) and library logs (FastAPI / SQLAlchemy / uvicorn) share one pipe and both reach Dash0 via the single `LoggingHandler`. One knob (`LOG_LEVEL`) gates both stdout and OTLP export.

**No `_redact_secrets`** — the key-name recursive scrubber was removed. The `SecretStr`-at-every-boundary convention self-masks secret values in `repr`/`str`/`model_dump`, making log-level redaction redundant. Callers must never log raw secret values; `SecretStr` is the enforcement.

**structlog processor chain** (shared across structlog-native and stdlib foreign records):
`merge_contextvars` → `_inject_trace_context` (pulls `trace_id`/`span_id` from active OTel span) → `add_log_level` → ISO-UTC timestamp → stack/exception formatters → `ConsoleRenderer` (non-prod) or `JSONRenderer` (prod). Per-mode renderer lives inside `ProcessorFormatter`.

**`shutdown()` registered on both registries** — registered at import time with `register_web_shutdown_hook` AND `register_worker_shutdown_hook` so every deploy path (web lifespan teardown + worker SIGTERM drain) force-flushes all three providers before the process exits. 30 s flush timeout per provider; `fly.production.toml` `kill_timeout` must exceed the total flush budget. None of this existed before — a running deploy previously dropped the last buffered batch.

**Wire-protocol trace propagation:**
- `current_traceparent()` — serializes active span context as W3C `traceparent`.
- `restore_traceparent_context(traceparent)` — parses back to OTel `Context`.
- `with_remote_parent_span(tracer, name, traceparent)` — emits a span under the remote trace; falls back to fresh trace on None/malformed.

`domain/intake/web.post_intake` records `current_traceparent()` when a webhook arrives and passes it into `core/workflow`; downstream tasks restore it. This is what gives one `trace_id` across webhook → terminal outcome.

**`spawn(name, coro)`** — `asyncio.create_task` wrapped with error logging. Task retained in a module-level set to prevent GC mid-flight.

**`SlowRequestLogMiddleware`** — emits `http.slow_request` warn for requests ≥ `SLOW_REQUEST_THRESHOLD_MS` (default 500ms). Never throws.

## Public interface

- `configure(role)` — initialize structlog + all three OTel providers. Call once at boot.
- `shutdown()` — async; force-flush + shut down all three providers. Registered with both shutdown registries automatically at import.
- `get_logger(name?)` — bound structlog logger.
- `spawn(name, coro)` — fire-and-forget background task with error logging.
- `current_traceparent()`, `restore_traceparent_context(tp)`, `with_remote_parent_span(tracer, name, tp)` — wire-protocol trace helpers.
- `SlowRequestLogMiddleware`, `SLOW_REQUEST_THRESHOLD_MS` — slow-request logging middleware.
- `active_task_count()` — number of in-flight spawned tasks (test helper).

## Entry points

- `apps/backend/app/core/observability/service.py` — configure + shutdown implementation.
- `apps/backend/app/core/observability/__init__.py` — public re-exports + shutdown hook registration.
- `apps/backend/app/core/observability/spawn.py` — spawn helper.
- `apps/backend/app/core/observability/traceparent.py` — wire-protocol helpers.
- `apps/backend/app/core/observability/slow_request.py` — slow-request middleware.
