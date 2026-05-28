# core/database

> Async SQLAlchemy engine, session factory, declarative base, and the in-house migration runner.

## Purpose

DB infrastructure layer every other module sits on. Owns the async engine and session factory (lazy singletons), the `Base` every module's ORM models inherit from, the `schema_migrations` tracking table, and an in-house migration runner that applies the migration list idempotently. No domain logic; no ORM models of its own.

## Public interface

Exports `Base`, `get_engine`, `get_sessionmaker`, `session`, `ping`, `ensure_schema_migrations_table`, `migrate`, `dispose`, `shutdown`, `set_test_session_override`, `truncate_all_tables`. See `apps/backend/app/core/database/__init__.py`.

- `Base` — declarative base; module models inherit.
- `session()` — async context manager yielding `AsyncSession`; caller decides commit/rollback. When a test override is installed, returns the override (so production code's `async with session() as s:` runs inside the test's transaction).
- `ping()` — `SELECT 1`, returns `bool`. Drives `/api/health`; swallows exceptions.
- `migrate()` — applies un-applied migrations. Idempotent.
- `dispose()` — closes engine, clears singletons.
- `shutdown()` — async alias for `dispose()`; self-registered with both web and worker shutdown registries at import time.
- `set_test_session_override(s)` — install (or clear) a fixture-bound `AsyncSession` so every production `session()` call routes to it. Used exclusively by the `db_session` test fixture.
- `truncate_all_tables(session) -> None` — empties every table in `Base.metadata` via per-table `DELETE FROM` in reverse-FK order. Uses DELETE rather than TRUNCATE so the reset endpoint doesn't block on lingering `AccessShare` readers (SSE streams, agent-gateway WS, background tasks). Sets a 2s `lock_timeout` so unexpected contention fails fast. yaaos PKs are UUIDs, no sequence reset needed. Callers must ensure model modules are imported first so their tables appear in metadata. The only allowed DB-wide reset primitive — used exclusively by the test reset path. Raises `RuntimeError` under `YAAOS_ENV=prod` (defence in depth alongside the HTTP route's `_guard_dev`). See [patterns.md § e2e seed paths use public APIs](patterns.md).

No HTTP routes.

## Module architecture

### Engine lifecycle

`get_engine()` lazily constructs `AsyncEngine` from `settings.database_url`. In `dev`/`test`, uses `NullPool` to avoid cross-event-loop contamination in `TestClient` tests (each test brings up a fresh loop). In `prod`, uses `QueuePool` sized via `db_pool_size` + `db_max_overflow` settings with `pool_pre_ping=True`. `get_sessionmaker()` produces sessions with `expire_on_commit=False`.

### Pool sizing

Pool size tracks **concurrent in-flight queries**, not event loops. A single asyncio loop can hold many connections at once: a Postgres connection is single-threaded at the protocol level (one query per connection in flight), so N coroutines `await`ing DB queries simultaneously need N connections. Defaults (`db_pool_size=10`, `db_max_overflow=5`) suit dev for both web and worker processes.

Worker rule of thumb: `db_pool_size >= WORKER_CONCURRENCY + 2` (one for the outbox drain, one headroom). Bump together when scaling worker concurrency. Web sizing tracks expected peak concurrent DB-touching requests. Production tunes both via env at deploy time.

### `ping()`

Health-check helper: runs `SELECT 1` in a session. Returns `True` on success, `False` on any exception — the endpoint reports a boolean, not a stack trace.

### Migrations

Alembic scaffolds files; the runner is custom. `migrate()` consults `schema_migrations` and applies any version not yet recorded. Stock `alembic upgrade` is forbidden.

The migration list is the `_MIGRATIONS` tuple in [service.py](../app/core/database/service.py); the per-migration body lives next to it. Each migration runs in its own transaction; on success the version inserts into `schema_migrations`. Re-running `migrate()` is a no-op for applied versions.

The `_apply_create_all` migration explicitly imports every module's `models` so the SQLAlchemy registry is populated before `create_all`. New modules with tables need adding to that import list — no auto-discovery.

Concurrent callers are serialized by a Postgres session-scoped advisory lock (`_MIGRATION_LOCK_KEY`) acquired on a dedicated connection that spans the whole call. The web process and worker both run `migrate()` on startup — and prod web will scale to multiple instances — so without the lock both readers can see an empty `applied` set, race on the DDL, and crash on the duplicate `INSERT INTO schema_migrations`. Followers block on the lock and re-read `applied` once they acquire it, finding everything already done. Session-scoped (not `pg_advisory_xact_lock`) because per-migration commits are separate transactions; the lock must outlive each. If a PgBouncer-style transaction-pooling proxy is ever placed in front of Postgres, this lock breaks (session affinity is lost between statements) — either bypass the pooler for `migrate()` or switch the lock primitive.

### `dispose()` / `shutdown()`

`dispose()` calls `engine.dispose()` and resets `_engine`/`_sessionmaker` to `None`, so the next call constructs a fresh engine (used by tests that swap DB URLs). `shutdown()` is the async wrapper registered with both web and worker shutdown registries at import time — no explicit caller required. See [patterns.md § Two process lifecycles, two registries](patterns.md).

## Data owned

- `schema_migrations` — `(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT NOW())`. Tracks applied migrations. No other module reads or writes it.

Every other table is owned by the module that defines its ORM model.

## How it's tested

`app/core/database/test/` carries focused tests for the pool kwargs, the transactional-rollback fixture, and the `migrate()` advisory-lock race. The rest of the module is exercised end-to-end by every integration test running through `TestClient` — `/api/health` covers `ping()`, and `migrate()` runs on every test-DB setup.

The `db_session` fixture in `apps/backend/conftest.py` is the standard transactional-rollback wrapper used by every integration test that hits Postgres. It opens an outer transaction, binds an `AsyncSession` to that connection, installs it via `set_test_session_override`, and uses a `restart_savepoint` listener so production-side `await s.commit()` calls become SAVEPOINT releases inside the outer transaction. Teardown rolls back the outer transaction — the test DB stays clean between cases without re-running migrations.
