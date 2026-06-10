"""Alembic env.py — dual-path migration runner.

Runs in two modes:

- **Boot path** (called via ``core/database.migrate()``): ``migrate()`` opens an
  async connection, bridges to sync via ``await conn.run_sync(_drive_alembic_upgrade)``,
  stashes the sync DBAPI connection on ``config.attributes["connection"]``, then calls
  ``alembic.command.upgrade(config, "head")``.  When ``run_migrations_online()`` sees
  a non-None ``config.attributes["connection"]`` it calls ``do_run_migrations`` directly
  with that stashed connection — no second engine is opened.

- **CLI path** (``alembic revision --autogenerate -m "<msg>"`` from terminal): no
  connection is stashed, so ``run_migrations_online()`` falls through to
  ``asyncio.run(run_async_migrations())``, which builds a fresh async engine from
  ``settings.database_url`` and bridges via ``await connection.run_sync(do_run_migrations)``.

The ``sqlalchemy.url`` in ``alembic.ini`` is ignored at runtime; ``env.py`` reads
``settings.database_url`` directly so dev / CI / prod all hit the right DB without
per-env ``alembic.ini`` edits.  The ini value remains only so misconfigured CLI
invocations fail loudly.
"""

from __future__ import annotations

import asyncio

# ---------------------------------------------------------------------------
# Discover and import every `app/**/models.py` so Base.metadata is fully
# populated before autogenerate runs. Filesystem is the source of truth —
# adding a new module's models.py is auto-detected, no env.py edit needed.
# Any models.py that fails to import crashes env.py loudly; that's the
# intended signal.
# ---------------------------------------------------------------------------
import importlib
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.database import Base

_BACKEND_ROOT = Path(__file__).resolve().parent.parent  # apps/backend/
_APP_ROOT = _BACKEND_ROOT / "app"
for _models_path in sorted(_APP_ROOT.rglob("models.py")):
    # Skip test fixtures and bytecode caches (none today, but defensive).
    if any(part in {"test", "__pycache__"} for part in _models_path.parts):
        continue
    _rel = _models_path.relative_to(_BACKEND_ROOT).with_suffix("")
    _module = ".".join(_rel.parts)
    importlib.import_module(_module)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    """Shared sync inner — configures Alembic context and runs migrations."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """CLI path: build a fresh async engine from settings.database_url and bridge to sync."""
    from app.core.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    # Build config section from the ini but override the URL with settings so
    # dev / CI / prod hit the right DB without per-env alembic.ini edits.
    ini_section = config.get_section(config.config_ini_section, {})
    ini_section["sqlalchemy.url"] = str(settings.database_url)
    connectable = async_engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    """--sql mode: emit raw SQL without a live connection (rarely used)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online mode: dual-path per boot vs. CLI caller."""
    connection = config.attributes.get("connection")
    if connection is not None:
        # Boot path: caller stashed a sync DBAPI connection via run_sync.
        do_run_migrations(connection)
    else:
        # CLI path: no stashed connection — build own async engine.
        # asyncio.run() only works from a sync (non-running-loop) context,
        # which is guaranteed for terminal `alembic` invocations.
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
