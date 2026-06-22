"""core/database — async SQLAlchemy engine + session factory + migration runner."""

from app.core.database.service import (
    Base,
    dispose,
    get_engine,
    get_sessionmaker,
    maintain_coding_agent_activity_partitions,
    migrate,
    ping,
    session,
    set_db_session_for_tests,
    shutdown,
    truncate_all_tables,
)

__all__ = [
    "Base",
    "dispose",
    "get_engine",
    "get_sessionmaker",
    "maintain_coding_agent_activity_partitions",
    "migrate",
    "ping",
    "session",
    "set_db_session_for_tests",
    "shutdown",
    "truncate_all_tables",
]

from app.core.shutdown_registry import register_web_shutdown_hook, register_worker_shutdown_hook

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
