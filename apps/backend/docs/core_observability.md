# core/observability

> structlog setup + conditional OTel SDK initialization.

## Purpose

Logging and tracing bootstrap. Configures structlog once at process start, picks renderer based on `YAAOS_ENV` (console-pretty in `dev`, JSON in `prod`), and conditionally wires the OpenTelemetry SDK with an OTLP gRPC exporter when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. Exposes `get_logger` — the standard logger factory used everywhere.

## Public interface

- `configure()` — initialize structlog and (when endpoint set) the OTel SDK. Idempotent. Called once from `main.py` at boot.
- `get_logger(name=None)` — returns a bound structlog logger. Used in place of `logging.getLogger`.
- `spawn(name, coro)` — fire-and-forget background work. Wraps `asyncio.create_task` with a try/except that logs `spawn.crashed`. Every long-running background coroutine goes through this; the wrapped task is also retained in a module-level set so the GC doesn't collect it mid-flight. Relocated from `core/primitives` in M04 Phase 6a — its job is exception logging in background tasks, an observability concern.
- `active_task_count()` — test helper; number of pending spawned tasks.

No HTTP routes. No tables. See `app/core/observability/__init__.py`.

## Module architecture

### structlog

Always configured. Reads `settings.log_level` and `settings.yaaos_env`, installs a processor chain: `merge_contextvars`, `add_log_level`, ISO-UTC `TimeStamper`, stack/exception formatters, then `ConsoleRenderer(colors=True)` in `dev` or `JSONRenderer` otherwise. Filtering happens in structlog via `make_filtering_bound_logger`; `cache_logger_on_first_use=True`. `logging.basicConfig` is also called so non-structlog logs land on stdout at the same level.

### OTel (conditional)

`Settings.otel_enabled` is true iff `otel_exporter_otlp_endpoint` is set. When enabled, `_configure_otel` lazily imports the SDK (so it never loads when disabled), builds a `TracerProvider` with `service.name = settings.otel_service_name`, adds a `BatchSpanProcessor` wrapping an `OTLPSpanExporter`, and sets the provider. Unset endpoint returns immediately. FastAPI and SQLAlchemy auto-instrumentation pick up the provider; this module doesn't wire them.

### Idempotency

Module-level `_initialized` flag guards repeat calls. The OTel SDK has its own global state — re-configure doesn't replace the provider.

## Data owned

None.

## How it's tested

`app/core/observability/test/` is a placeholder. Smoke-tested via `/api/health` and by every integration test (each runs `configure()` at startup).
