# core/database

> Async SQLAlchemy engine, session factory, declarative base, and the in-house migration runner.

## Purpose

DB infrastructure layer every other module sits on. Owns the async engine and session factory (lazy singletons), the `Base` every module's ORM models inherit from, the `schema_migrations` tracking table, and an in-house migration runner that applies the migration list idempotently. No domain logic; no ORM models of its own.

## Public interface

Exports `Base`, `get_engine`, `get_sessionmaker`, `session`, `ping`, `ensure_schema_migrations_table`, `migrate`, `dispose`. See `apps/backend/app/core/database/__init__.py`.

- `Base` — declarative base; module models inherit.
- `session()` — async context manager yielding `AsyncSession`; caller decides commit/rollback.
- `ping()` — `SELECT 1`, returns `bool`. Drives `/api/health`; swallows exceptions.
- `migrate()` — applies un-applied migrations. Idempotent.
- `dispose()` — shutdown hook; closes engine, clears singletons.

No HTTP routes.

## Module architecture

### Engine lifecycle

`get_engine()` lazily constructs `AsyncEngine` from `settings.database_url`. In `dev`, uses `NullPool` to avoid cross-event-loop contamination in `TestClient` tests (each test brings up a fresh loop). In `prod`, default pool with `pool_pre_ping=True`. `get_sessionmaker()` produces sessions with `expire_on_commit=False`.

### `ping()`

Health-check helper: runs `SELECT 1` in a session. Returns `True` on success, `False` on any exception — the endpoint reports a boolean, not a stack trace.

### Migrations

Alembic scaffolds files; the runner is custom. `migrate()` consults `schema_migrations` and applies any version not yet recorded. Stock `alembic upgrade` is forbidden.

Migration list (in order):

1. `001_create_all_m01` — imports every module's `models`, calls `Base.metadata.create_all`.
2. `002_github_settings_slug` — adds `slug` column to `github_settings`.
3. `003_drop_repos_table` — drops `repos`; converts dependents from FK(`repo_id`) → string(`repo_external_id`), backfilling first.
4. `004_review_jobs_triggered_by_destination` — adds `triggered_by` (default `'pr_ready'`) and `destination` (default `'vcs'`) to `review_jobs`. Promotes the audit-only `trigger_reason` to a queryable column; preps the row for future `run_review` callers that don't post to VCS.
5. `005_drop_reviewer_agents` — drops the per-agent `reviewer_agents` table; one row per (PR × review run) is now sufficient.
6. `006_review_jobs_activity_log_model_effort` — adds `activity_log JSONB DEFAULT '[]'`, `model TEXT`, `effort TEXT`; drops `cost_usd`. Activity log captures pre-rendered Claude Code stream events; cost is not tracked because CLI pricing data is not authoritative.
7. `007_create_durable_findings_tables` — creates `findings`, `finding_observations`, `comment_threads`, `comment_messages`, `acknowledgment_decisions` (plan/notes/full-pr-flow.md §4.1). Idempotent CREATE TABLE IF NOT EXISTS via `Base.metadata.create_all` on just these tables. Tables are reviewer-owned; FKs from generation-2 tables land in §13 step 7 when `review_jobs` is renamed `reviews`.

Each migration runs in its own transaction; on success the version inserts into `schema_migrations`. Re-running `migrate()` is a no-op for applied versions.

The `_apply_create_all` migration explicitly imports every module's `models` so the SQLAlchemy registry is populated before `create_all`. New modules with tables need adding to that import list — no auto-discovery.

### `dispose()`

Called on shutdown. `engine.dispose()` and resets `_engine`/`_sessionmaker` to `None`, so the next request constructs a fresh engine (used by tests that swap DB URLs).

## Data owned

- `schema_migrations` — `(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW())`. Tracks applied migrations. No other module reads or writes it.

Every other table is owned by the module that defines its ORM model.

## How it's tested

`app/core/database/test/` is a placeholder. Module is exercised end-to-end by every integration test running through `TestClient` — `/api/health` covers `ping()`, and `migrate()` runs on every test-DB setup.
