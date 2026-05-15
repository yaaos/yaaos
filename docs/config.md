# `core/config`

Boot-time configuration via pydantic-settings. Reads from process env and `.env*` files (multi-file precedence per `pydantic-settings`).

## Public interface

```python
from app.core.config import Settings, get_settings
```

- `Settings` — Pydantic model holding every boot-time env var. Required fields raise on construction if unset.
- `get_settings()` — cached singleton accessor. Use this everywhere; do not instantiate `Settings()` directly.

## Owned data

None — config is read-only and stateless.

## Required env vars

`DATABASE_URL`, `YAAOF_ENCRYPTION_KEY`. Everything else has a default. See `../.env.sample` and `plan/milestones/M01-code-review/architecture.md` § Boot-time environment variables for the canonical list.

## Tests

`app/core/config/test/` — integration tests for env-var parsing and defaults. The `get_settings.cache_clear()` call is the standard way to reset the singleton between tests that monkeypatch env.
