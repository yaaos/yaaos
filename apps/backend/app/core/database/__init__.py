"""core/database — async SQLAlchemy engine + session factory + migration runner."""

from app.core.database.service import (
    Base,
    dispose,
    ensure_schema_migrations_table,
    get_engine,
    get_sessionmaker,
    migrate,
    ping,
    session,
    shutdown,
    truncate_all_tables,
)

__all__ = [
    "Base",
    "dispose",
    "ensure_schema_migrations_table",
    "get_engine",
    "get_sessionmaker",
    "migrate",
    "ping",
    "session",
    "shutdown",
    "truncate_all_tables",
]

from app.core.shutdown_registry import register_web_shutdown_hook, register_worker_shutdown_hook

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
