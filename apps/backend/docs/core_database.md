# core/database

> Async SQLAlchemy engine, session factory, declarative base, and the Alembic-based migration runner.

## Scope

- Owns: `AsyncEngine` + `AsyncSessionmaker` singletons, `Base`, `alembic_version` table (managed by Alembic), migration runner.
- Does NOT own: any domain ORM model — those belong to the modules that define them.

## Why / invariants

**`NullPool` in dev/test** — avoids cross-event-loop contamination (`TestClient` brings up a fresh loop per test). `QueuePool` in prod with `pool_pre_ping=True`.

**Pool sizing rule of thumb** — pool size tracks concurrent in-flight queries, not connections or coroutines. For the worker: `db_pool_size >= WORKER_CONCURRENCY + 2`. Tune via env at deploy time; defaults (`db_pool_size=10`, `db_max_overflow=5`) suit dev.

**Engine version guard** — `migrate()` opens a throwaway connection and calls `SHOW server_version_num` before touching any DDL. Raises `RuntimeError` with a readable message if the engine is older than Postgres 18. The helper `_assert_min_pg_version(raw)` is pure and unit-tested independently.

**Migration advisory lock** — `migrate()` holds a Postgres session-scoped advisory lock (`_MIGRATION_LOCK_KEY`) for the full call, spanning per-migration commits. Prevents web + worker both racing to apply the same migration on startup. **Warning:** session-scoped locks break under PgBouncer transaction-pooling (session affinity is lost between statements). If a connection pooler is ever added, bypass it for `migrate()` or switch lock primitive.

**Alembic migration runner** — `migrate()` calls `alembic upgrade head` via `alembic.command.upgrade` (programmatic API). The connection is stashed on `cfg.attributes["connection"]` so `env.py`'s boot path reuses the caller's sync connection — no second engine opened. `alembic/versions/0001_baseline.py` is the single baseline revision; new DDL is added as subsequent numbered revisions. `script_location` is overridden to an absolute path so `migrate()` is cwd-independent. Use `alembic revision --autogenerate -m "..."` (CLI) to generate new revisions; direct `alembic upgrade` on disk is not used at runtime.

**`set_test_session_override`** (in `app.core.database.service`, not re-exported from the package) — routes every `async with session()` call to the fixture-bound `AsyncSession` so production code runs inside the test's outer transaction. The `db_session` fixture in `conftest.py` imports it directly from the submodule. The `restart_savepoint` listener turns production `await s.commit()` into a SAVEPOINT release; teardown rolls back the outer transaction.

**UUID primary keys via `uuidv7()`** — Postgres 18 ships `uuidv7()` natively. Every UUID PK column carries `server_default=text("uuidv7()")` so the DB mints a time-ordered UUID v7 on INSERT. Services never pass `id=` to Row constructors. See `apps/backend/docs/patterns.md` § UUID primary keys for the full convention and the semgrep enforcer.

**`truncate_all_tables`** — uses `DELETE FROM` (not `TRUNCATE`) to avoid blocking SSE/WS/background `AccessShare` readers. Sets `lock_timeout=2s`. UUID PKs mean no sequence reset needed. Raises `RuntimeError` in `prod`. Only used by the test-reset path; never call elsewhere.

**Partitioned tables — DDL lives here.** Raw partition DDL (`PARTITION BY RANGE`, per-week `PARTITION OF`) is confined to `core/database` because it needs raw `text(...)` SQL with interpolated partition names + range bounds; `bin/check_table_access` allowlists only `core/database/**` for cross-table raw SQL. The owning module's mapped class lives on the shared `Base` and declares `postgresql_partition_by` (e.g. `CodingAgentActivityRow`), so `Base.metadata.create_all` emits the partitioned parent — keeping the ORM column shape and the Alembic baseline DDL from drifting. The `coding_agent_activity` partitioned parent is created by the Alembic baseline (`0001_baseline.py`) via raw `op.execute` (Alembic autogen does not handle `PARTITION BY RANGE`). Child partitions are seeded from `migrate()` by calling `maintain_coding_agent_activity_partitions()` immediately after Alembic finishes; the same function runs daily from the scheduled task in `core/coding_agent`. Partition naming, week alignment, and the seed window `(0, +1, +2)` are one source of truth — the `_coding_agent_activity_partition_ddl` helper shared by the baseline and the maintenance task. Every `CREATE TABLE` uses `IF NOT EXISTS`. `coding_agent_activity` is the codebase's first partitioned table.

## Gotchas

- `expire_on_commit=False` on all sessions — attributes stay accessible after commit without an extra round-trip.
- `dispose()` resets the singleton so the next call constructs a fresh engine (used by tests swapping DB URLs).

