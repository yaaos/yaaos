# core/database

> Async SQLAlchemy engine, session factory, declarative base, and the in-house migration runner.

## Scope

- Owns: `AsyncEngine` + `AsyncSessionmaker` singletons, `Base`, `schema_migrations` table, migration runner.
- Does NOT own: any domain ORM model — those belong to the modules that define them.

## Why / invariants

**`NullPool` in dev/test** — avoids cross-event-loop contamination (`TestClient` brings up a fresh loop per test). `QueuePool` in prod with `pool_pre_ping=True`.

**Pool sizing rule of thumb** — pool size tracks concurrent in-flight queries, not connections or coroutines. For the worker: `db_pool_size >= WORKER_CONCURRENCY + 2`. Tune via env at deploy time; defaults (`db_pool_size=10`, `db_max_overflow=5`) suit dev.

**Migration advisory lock** — `migrate()` holds a Postgres session-scoped advisory lock (`_MIGRATION_LOCK_KEY`) for the full call, spanning per-migration commits. Prevents web + worker both racing to apply the same migration on startup. **Warning:** session-scoped locks break under PgBouncer transaction-pooling (session affinity is lost between statements). If a connection pooler is ever added, bypass it for `migrate()` or switch lock primitive.

**Custom migration runner** — `_MIGRATIONS` tuple in `service.py` is the sole source of truth. `alembic upgrade head` is forbidden; the runner applies idempotently. `_apply_create_all` explicitly imports every module's `models` — new modules with tables must be added to that import list.

**`set_test_session_override`** — routes every `async with session()` call to the fixture-bound `AsyncSession` so production code runs inside the test's outer transaction. The `db_session` fixture uses a `restart_savepoint` listener so production `await s.commit()` becomes a SAVEPOINT release; teardown rolls back the outer transaction.

**`truncate_all_tables`** — uses `DELETE FROM` (not `TRUNCATE`) to avoid blocking SSE/WS/background `AccessShare` readers. Sets `lock_timeout=2s`. UUID PKs mean no sequence reset needed. Raises `RuntimeError` in `prod`. Only used by the test-reset path; never call elsewhere.

## Gotchas

- `expire_on_commit=False` on all sessions — attributes stay accessible after commit without an extra round-trip.
- `dispose()` resets the singleton so the next call constructs a fresh engine (used by tests swapping DB URLs).

