"""Alembic env.py — used only for `alembic revision --autogenerate` scaffolding.

The actual migration runner is `core/database.migrate()` (invoked via
`bin/migrate`). Do NOT use `alembic upgrade` directly — it bypasses the
`schema_migrations` per-migration tracking model.

See `docs/patterns.md` § Per-migration tracking.
"""

from logging.config import fileConfig

from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No target metadata yet — no ORM models in the skeleton. Add when modules
# define `Base.metadata` extensions: `target_metadata = [Base.metadata]`.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # lazy: sqlalchemy engine_from_config is only needed in online mode
    from sqlalchemy import engine_from_config, pool  # noqa: PLC0415

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
