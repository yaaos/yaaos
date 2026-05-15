# `core/observability`

structlog setup + conditional OTel SDK initialization.

## Public interface

```python
from app.core.observability import configure, get_logger
```

- `configure()` — initialize structlog and (when `OTEL_EXPORTER_OTLP_ENDPOINT` is set) the OTel SDK. Idempotent. Called once from `main.py` at boot.
- `get_logger(name=None)` — return a bound structlog logger.

## OTel behavior

- **Unset `OTEL_EXPORTER_OTLP_ENDPOINT`:** OTel SDK is never initialized. No spans exported. No boot failure. (Silent disable.)
- **Set `OTEL_EXPORTER_OTLP_ENDPOINT`:** SDK initialized with an OTLP gRPC exporter pointed at the endpoint; FastAPI + SQLAlchemy auto-instrumentation kicks in via the contrib packages.

## Owned data

None.

## Tests

Smoke-tested via `/api/health` (which uses `get_logger`). Dedicated tests land when log assertions become useful.
