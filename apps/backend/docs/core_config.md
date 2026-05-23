# core/config

> Boot-time configuration via pydantic-settings — env vars and `.env*` files into a single typed `Settings`.

## Purpose

Single source of truth for every boot-time configuration value. Reads process env, falling back to `.env` files in multi-file precedence. Required fields raise at construction if unset; optional fields have hardcoded defaults. Read-only and stateless.

## Public interface

Exports `Settings` (pydantic `BaseSettings`) and `get_settings()` (cached singleton). Tests reset with `get_settings.cache_clear()` after monkeypatching env. See `apps/backend/app/core/config/__init__.py`.

No HTTP routes. No tables.

## Module architecture

### Settings fields

Required (construction fails if unset):
- `database_url` — async Postgres URL (`postgresql+asyncpg://...`).
- `yaaos_encryption_key` — Fernet key (32-byte URL-safe base64) for credential encryption.

Optional, with defaults:
- `yaaos_env: Literal["dev", "test", "prod"]` (default `prod`).
- `yaaos_port` (8080).
- `yaaos_cors_origins` — comma-separated; honored only when env is not `dev`.
- `db_pool_size` (10), `db_max_overflow` (5) — SQLAlchemy QueuePool sizing for the prod-path engine (see [core/database](core_database.md) § Pool sizing).
- `otel_exporter_otlp_endpoint`, `otel_service_name` (`yaaos`).
- `log_level` (`INFO`).
- `github_api_base_url` (`https://api.github.com`; overridden by e2e stack to `apps/fake-github`).
- Time controls: `yaaos_review_debounce_seconds` (30), `yaaos_reaper_interval_seconds` (30), `yaaos_heartbeat_interval_seconds` (10).

### `.env` file precedence

`SettingsConfigDict` reads, in order: `.env`, `.env.local`, `.env.dev`, `.env.dev.local`. Later overrides earlier; process env overrides everything. `extra="ignore"` so unknown vars don't error.

### Derived properties

- `cors_origins_list` — `["*"]` when `yaaos_env == "dev"`; otherwise parsed `yaaos_cors_origins` (empty if unset).
- `otel_enabled` — true iff `otel_exporter_otlp_endpoint` is set.

### Why a cached singleton

`Settings()` parses env on every call; `@cache` on `get_settings()` makes subsequent calls free. Callers never instantiate `Settings` directly. Tests monkeypatching env call `get_settings.cache_clear()` to force a fresh parse.

## Data owned

None. Read-only and stateless.

## How it's tested

`app/core/config/test/` — integration tests for env parsing and defaults. Standard pattern: `monkeypatch.setenv(...)` then `get_settings.cache_clear()`.
