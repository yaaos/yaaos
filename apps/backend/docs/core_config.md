# core/config

> Boot-time configuration via pydantic-settings — env vars and `.env*` files into a single typed `Settings`.

## Scope

- Owns: `Settings` (pydantic `BaseSettings`), `get_settings()` cached singleton.
- Read-only and stateless. No HTTP routes, no tables.

## Why / invariants

**Required fields raise at construction** — `database_url`, `yaaos_encryption_key`, `redis_url`, `yaaos_public_hostname` must be set; absence crashes boot immediately with a pydantic `ValidationError`.

**All sensitive fields are `SecretStr`** — `repr`, `model_dump`, and `model_dump_json` all render as `'**********'`. Call `.get_secret_value()` only at the byte boundary (Fernet construction, JWT sign, HTTP Authorization header). Verified by `test_secret_redaction.py`.

**`.env` file precedence** — `.env` → `.env.local` → `.env.dev` → `.env.dev.local`. Later overrides earlier; process env overrides all. `extra="ignore"` so unknown vars don't error.

**Cached singleton** — `Settings()` parses env on every call; `@cache` on `get_settings()` makes subsequent calls free. Tests monkeypatching env must call `get_settings.cache_clear()` afterward.

**`YAAOS_PUBLIC_HOSTNAME`** — required. Canonical public hostname of this backend deployment (e.g. `app.yaaos.cloud`; no scheme, no path). Boot fails with a `ValidationError` when unset. Used by `core/agent_gateway` to validate the `X-Yaaos-Audience` header in agent identity-exchange requests. Must match what `hostFromURL(YAAOS_BACKEND_URL)` produces on the agent side.

## Gotchas

- Callers never instantiate `Settings` directly — always via `get_settings()`.
- `cors_origins_list` returns `["*"]` when `yaaos_env == "dev"`; otherwise parsed `YAAOS_CORS_ORIGINS`.

