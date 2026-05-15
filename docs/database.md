# `core/database`

Async SQLAlchemy engine, session factory, and `schema_migrations` bootstrap.

## Public interface

```python
from app.core.database import Base, get_engine, get_sessionmaker, session, ping, ensure_schema_migrations_table, dispose
```

- `Base` — declarative base. Module models inherit from this.
- `get_engine()`, `get_sessionmaker()` — lazy singletons.
- `session()` — async context manager yielding an `AsyncSession`. Caller decides commit/rollback.
- `ping()` — `SELECT 1`. Returns `bool`. Used by `/api/health`.
- `ensure_schema_migrations_table()` — idempotent bootstrap of the per-migration tracking table. Called by `core/webserver`'s lifespan at boot.
- `dispose()` — shutdown hook; closes the engine.

## Owned data

- The `schema_migrations` tracking table (created idempotently at boot; rows added by the migration runner, not by Alembic directly).
- No domain tables yet. Future modules add their tables via Alembic migration files under `apps/backend/alembic/versions/`.

## Migrations

`alembic` is used **only** to scaffold migration files (`alembic revision --autogenerate -m "..."`). The actual runner is `core/database.migrate()` (to be added when first migration lands) — it reads `schema_migrations` and applies files not yet tracked. **Stock `alembic upgrade` is forbidden.**

## Tests

The skeleton has no `core/database` tests of its own — `/api/health` exercises `ping()` end-to-end. Future modules' integration tests run inside a transaction rolled back at teardown (per `patterns.md` § Integration tests).
