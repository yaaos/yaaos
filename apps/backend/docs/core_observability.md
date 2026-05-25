# core/observability

> structlog + always-on OTel SDK initialization.

## Purpose

Logging + tracing bootstrap. Configures structlog once at process start (console-pretty in `dev`, JSON in `prod`) and **always** wires the OpenTelemetry SDK — `TracerProvider` + W3C trace-context propagator + FastAPI/SQLAlchemy auto-instrumentation. When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, spans flow to that endpoint via a `BatchSpanProcessor`; when unset spans are created and discarded. structlog log records carry `trace_id` + `span_id` whenever a span is active.

## Public interface

- `configure()` — initialize structlog + OTel SDK. Idempotent. Called once from `main.py` at boot.
- `get_logger(name=None)` — returns a bound structlog logger. Used in place of `logging.getLogger`.
- `spawn(name, coro)` — fire-and-forget background work. Wraps `asyncio.create_task` with a try/except that logs `spawn.crashed`. The wrapped task is retained in a module-level set so the GC doesn't collect it mid-flight.
- `active_task_count()` — test helper; number of pending spawned tasks.
- `SlowRequestLogMiddleware` — ASGI middleware that emits a `http.slow_request` warn log for any request taking ≥ `SLOW_REQUEST_THRESHOLD_MS` (default 500). Mounted by `core/webserver` on every app. Forensic trail for intermittent slow responses; never throws.

No HTTP routes. No tables. See `app/core/observability/__init__.py`.

## Module architecture

### structlog

Reads `settings.log_level` and `settings.yaaos_env`. Processor chain: `merge_contextvars`, `_inject_trace_context` (— pulls `trace_id` + `span_id` from the active OTel span), `add_log_level`, ISO-UTC `TimeStamper`, stack/exception formatters, then `ConsoleRenderer(colors=True)` in `dev` or `JSONRenderer` otherwise. `logging.basicConfig` is also called so non-structlog logs land on stdout at the same level.

### OTel — always on, exporter optional

`_configure_otel` (called from `configure()` regardless of endpoint):

- Builds a `TracerProvider` with `service.name` set per role: `settings.otel_service_name_app` (web process, default `yaaos-app`) or `settings.otel_service_name_worker` (taskiq worker, default `yaaos-worker`). `configure(role=...)` picks the field — `app/main.py` calls it with `"app"`, `app/core/tasks/worker.py` with `"worker"`.
- Sets `TraceContextTextMapPropagator` as the global propagator so W3C `traceparent` headers cross every HTTP boundary by default.
- Attaches a `BatchSpanProcessor(OTLPSpanExporter(endpoint))` **only when** `otel_exporter_otlp_endpoint` is set. Without an endpoint, spans are still created and discarded — code that emits spans never needs feature flags.
- Runs `FastAPIInstrumentor().instrument()` + `SQLAlchemyInstrumentor().instrument()`. Both calls swallow "already instrumented" errors so test reloads stay benign.

### No exporter in prod yet

The boot path always sets up the SDK so wire-protocol code (`traceparent` propagation, structlog correlation, ActivityEvent linkage) can rely on it without flags. Adding a real exporter in prod later (Datadog / Honeycomb / Tempo) is a single env-var flip — no code change.

### Wire-protocol trace propagation

Three helpers bridge the in-process OTel SDK and the `traceparent` strings the wire protocol carries:

- `current_traceparent() -> str | None` — serializes the active span context as a W3C `traceparent`. Returns `None` outside any span.
- `restore_traceparent_context(traceparent) -> Context | None` — parses a `traceparent` and returns the OTel `Context` whose active span points at the remote span.
- `with_remote_parent_span(tracer, name, traceparent)` — context manager that emits a span sharing the trace id of `traceparent`. Falls back to a fresh trace when `traceparent` is None / malformed.

`domain/intake/web.post_intake` records `current_traceparent()` when a webhook arrives and passes it into `core/workflow.get_engine().start(traceparent=...)`. The workflow execution row stamps `otel_trace_context`; downstream tasks restore that context when they emit work-spans. The architecture's "one trace_id covers webhook → ... → terminal outcome" property rides on this thread.

The helpers are unit-tested; task-body span emission (per-`start_step`, per-`route_workflow`, per-`handle_agent_event`) is not yet wired into the task bodies.

### Idempotency

Module-level `_initialized` flag guards repeat `configure()` calls. The OTel SDK has its own global state — a second `set_tracer_provider` is a no-op; the instrument-once try/except in `_configure_otel` absorbs the duplicate-instrumentation exceptions raised by FastAPI/SQLAlchemy instrumentors on test reload.

## Data owned

None.

## How it's tested

`app/core/observability/test/test_otel.py` covers: `_inject_trace_context` adds `trace_id`/`span_id` only when a span is active, hex widths are correct, and the in-memory `SpanExporter` fixture captures emitted spans. Real exporter wiring is verified indirectly — every integration test runs `configure()` and the auto-instrumentation emits spans on every request.

The in-memory exporter pattern (attach an `InMemorySpanExporter` via `SimpleSpanProcessor` for a test) is the recommended way to assert on span shape — see the `in_memory_spans` fixture for an example.
