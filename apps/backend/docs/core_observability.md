# core/observability

> structlog + always-on OTel SDK initialization.

## Scope

- Owns: structlog config, OTel `TracerProvider` + propagator + auto-instrumentation, `spawn()`, `SlowRequestLogMiddleware`, wire-protocol trace helpers.
- No HTTP routes, no tables.

## Why / invariants

**OTel always on, exporter optional** — SDK + spans are always initialized. `BatchSpanProcessor(OTLPSpanExporter)` attached only when `otel_exporter_otlp_endpoint` is set. Adding a real exporter in prod is a single env-var flip; no code change or feature flags.

**`configure(role=...)` must be called once at boot** — `"app"` from `web.py`, `"worker"` from `core/tasks/runtime.py`. Sets `service.name` accordingly. Idempotent (module-level `_initialized` flag + OTel "already instrumented" guard).

**Wire-protocol trace propagation:**
- `current_traceparent()` — serializes active span context as W3C `traceparent`.
- `restore_traceparent_context(traceparent)` — parses back to OTel `Context`.
- `with_remote_parent_span(tracer, name, traceparent)` — emits a span under the remote trace; falls back to fresh trace on None/malformed.

`domain/intake/web.post_intake` records `current_traceparent()` when a webhook arrives and passes it into `core/workflow`; downstream tasks restore it. This is what gives one `trace_id` across webhook → terminal outcome.

**structlog processor chain:** `merge_contextvars` → `_inject_trace_context` (pulls `trace_id`/`span_id` from active OTel span) → `add_log_level` → ISO-UTC timestamp → stack/exception formatters → `ConsoleRenderer` (dev) or `JSONRenderer` (prod).

**`spawn(name, coro)`** — `asyncio.create_task` wrapped with error logging. Task retained in a module-level set to prevent GC mid-flight.

**`SlowRequestLogMiddleware`** — emits `http.slow_request` warn for requests ≥ `SLOW_REQUEST_THRESHOLD_MS` (default 500ms). Never throws.

