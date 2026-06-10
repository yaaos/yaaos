"""SQLAlchemy async engine + session factory + migration runner.

Boots the schema via Alembic (``migrate()``) and owns the async engine
and session factory the rest of the backend uses.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry.instrumentation.utils import suppress_instrumentation
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    """SQLAlchemy declarative base. Module models inherit from this."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# Test override. When set (by the transactional-rollback pytest fixture in
# `conftest.py`), every call to `session()` yields this fixture-bound session
# instead of opening a new one. The fixture wraps the entire test in an
# outer transaction with SAVEPOINTs so production `await s.commit()` calls
# don't actually flush to disk — they create nested savepoints inside the
# rolled-back outer transaction. Production code never sets this; the
# default-None branch is the prod path.
_test_session_override: ContextVar[AsyncSession | None] = ContextVar(
    "yaaos_test_session_override", default=None
)


def set_test_session_override(s: AsyncSession | None) -> None:
    """Install a session that `session()` will yield. Test-only."""
    _test_session_override.set(s)


def _engine_kwargs(settings) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """Build create_async_engine kwargs from settings.

    Dev/test → NullPool (avoids cross-event-loop contamination in TestClient
    tests where each test brings up a fresh loop). Prod → QueuePool sized
    from settings, with pool_pre_ping to weed stale connections.
    """
    kwargs: dict[str, object] = {"future": True}
    if settings.is_non_prod:
        from sqlalchemy.pool import NullPool  # noqa: PLC0415

        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    return kwargs


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, **_engine_kwargs(settings))  # type: ignore[arg-type]
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an async session. Caller decides commit/rollback boundaries.

    When the test transactional fixture is active (see `conftest.py`
    `db_session`), all calls return the fixture-bound session so every
    production write happens inside the test's outer transaction and gets
    rolled back at teardown.
    """
    override = _test_session_override.get()
    if override is not None:
        yield override
        return
    async with get_sessionmaker()() as s:
        yield s


async def ping() -> bool:
    """`SELECT 1` against the DB. Returns True on success, False on any error.

    Used by `/api/health` (web) and `/health` (worker) to report DB
    connectivity. Swallows all exceptions intentionally — the endpoint reports a
    boolean, not a stack trace. Runs inside `suppress_instrumentation()` so the
    constant health-probe queries don't emit a SQLAlchemy span apiece.
    """
    try:
        with suppress_instrumentation():
            async with session() as s:
                await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# Postgres advisory lock key for `migrate()`. Arbitrary stable bigint — pick
# any value that doesn't collide with another advisory lock; this codebase
# has no other advisory-lock users.
_MIGRATION_LOCK_KEY = 0x7AA05_DB_5C_4E11A


_MIN_PG_VERSION_NUM = 180000
_MIN_PG_MAJOR = 18


def _assert_min_pg_version(server_version_num: str) -> None:
    """Raise RuntimeError if server_version_num (e.g. '170004') is below the minimum.

    Accepts the integer string returned by ``SHOW server_version_num``.
    Called at the top of ``migrate()`` so a wrong engine fails before any
    DDL is touched.
    """
    actual = int(server_version_num)
    if actual < _MIN_PG_VERSION_NUM:
        actual_major = actual // 10000
        raise RuntimeError(
            f"yaaos requires Postgres {_MIN_PG_MAJOR} or later; "
            f"connected engine reports version {actual_major} "
            f"(server_version_num={server_version_num}). "
            f"Upgrade the database engine to Postgres {_MIN_PG_MAJOR}."
        )


def _drive_alembic_upgrade(sync_conn) -> None:  # type: ignore[no-untyped-def]
    """Sync callback passed to ``conn.run_sync`` — drives ``alembic upgrade head``.

    Resolves paths from ``__file__`` so the call is cwd-independent;
    ``migrate()`` may be called from the web process or the worker process with
    different cwds.  Stashes the sync DBAPI connection on the Alembic config so
    ``env.py``'s boot path uses it directly rather than opening a second engine.
    """
    import alembic.command  # noqa: PLC0415
    import alembic.config  # noqa: PLC0415

    # service.py lives at apps/backend/app/core/database/service.py.
    # Climb 4 levels up to reach apps/backend/.
    backend_root = Path(__file__).resolve().parents[3]
    ini_path = backend_root / "alembic.ini"
    cfg = alembic.config.Config(str(ini_path))
    # Override script_location with an absolute path so cwd doesn't matter.
    # alembic.ini stores "alembic" as a relative path; resolve it against the ini file's directory.
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.attributes["connection"] = sync_conn
    alembic.command.upgrade(cfg, "head")


async def migrate() -> None:
    """Apply any pending Alembic revisions. Idempotent and concurrency-safe.

    Asserts the engine is Postgres >= 18 before touching any DDL — a wrong
    engine fails loudly here rather than deep inside a migration.

    Serializes via a Postgres session-scoped advisory lock held on a dedicated
    connection that spans the whole Alembic upgrade.  Two processes starting at
    once (web + worker) both call ``migrate()``; whichever acquires the lock
    first applies, the other blocks, then Alembic reads ``alembic_version`` and
    finds it is already at head.

    The advisory lock is on a *dedicated* connection (``lock_conn``), separate
    from the connection passed to ``run_sync``.  The lock must outlive
    Alembic's per-revision transactions; a shared connection would release the
    lock when Alembic commits.

    After ``alembic upgrade head`` returns, ``maintain_coding_agent_activity_partitions``
    seeds the current ISO-week child partition and the next two for the
    partitioned ``coding_agent_activity`` table.
    """
    async with get_engine().connect() as conn:
        result = await conn.execute(text("SHOW server_version_num"))
        _assert_min_pg_version(result.scalar_one())
    async with get_engine().connect() as lock_conn:
        await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
        try:
            async with get_engine().connect() as conn:
                await conn.run_sync(_drive_alembic_upgrade)
            await maintain_coding_agent_activity_partitions()
            logging.getLogger(__name__).info("migration.done")
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})


# Window of ISO-week offsets seeded/maintained for `coding_agent_activity`:
# the current week plus the next two. The Alembic baseline and the daily
# maintenance task both use this same window so a fresh DB and a long-running
# one have identical create-ahead.
_CODING_AGENT_ACTIVITY_WINDOW_OFFSETS = (0, 1, 2)


def _coding_agent_activity_week_start(now: datetime) -> datetime:
    """UTC Monday 00:00:00 of the ISO week containing `now`.

    The single anchor every partition-name/range computation derives from, so
    naming stays consistent across the Alembic baseline and the maintenance task.
    """
    today_midnight = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return today_midnight - timedelta(days=today_midnight.weekday())


def _coding_agent_activity_partition_ddl(now: datetime) -> list[str]:
    """`CREATE TABLE IF NOT EXISTS … PARTITION OF` statements for the window.

    One weekly child partition per offset in
    `_CODING_AGENT_ACTIVITY_WINDOW_OFFSETS` (current week → +2). Bounds run
    from week-start (Monday UTC 00:00:00) to next-week-start (exclusive). Names
    are `coding_agent_activity_pYYYYWW` using ISO year-week for deterministic
    ordering. The single source of truth for partition naming + range bounds —
    shared by the Alembic baseline and the maintenance task.

    Bounds and the partition name derive from server-side `now` + a fixed
    ISO-week format — no caller data ever reaches these strings, so the
    f-string interpolation is injection-free.
    """
    week_start = _coding_agent_activity_week_start(now)
    statements: list[str] = []
    for offset in _CODING_AGENT_ACTIVITY_WINDOW_OFFSETS:
        lower = week_start + timedelta(weeks=offset)
        upper = lower + timedelta(weeks=1)
        iso_year, iso_week, _ = lower.isocalendar()
        partition_name = f"coding_agent_activity_p{iso_year:04d}{iso_week:02d}"
        lower_lit = lower.strftime("%Y-%m-%d %H:%M:%S+00")
        upper_lit = upper.strftime("%Y-%m-%d %H:%M:%S+00")
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {partition_name} "
            f"PARTITION OF coding_agent_activity "
            f"FOR VALUES FROM ('{lower_lit}') TO ('{upper_lit}')"
        )
    return statements


async def maintain_coding_agent_activity_partitions() -> None:
    """Rolling create-ahead + drop maintenance for `coding_agent_activity`.

    Creates child partitions for the current ISO-UTC week and the next two
    weeks (3 partitions total, ~2-week create-ahead window), and drops
    partitions whose week is older than 4 weeks before the current week
    (the table's documented retention). Idempotent: `CREATE TABLE IF NOT
    EXISTS` for create, `DROP TABLE IF EXISTS` for drop, repeated runs on
    the same day are no-ops.

    Raw partition DDL lives here (not in `core/coding_agent`) because
    `core/database` is the only module the table-access checker allows
    raw SQL on the `coding_agent_activity` parent. The companion
    `@scheduled` task in `core/coding_agent` calls this function once a
    day.

    Partition naming + week alignment match the Alembic baseline (which seeds
    the same window via the shared `_coding_agent_activity_partition_ddl` helper):
    UTC Monday 00:00 week start, `coding_agent_activity_pYYYYWW` using ISO-year-week.
    """
    now = datetime.now(UTC)
    week_start = _coding_agent_activity_week_start(now)

    engine = get_engine()
    async with engine.begin() as conn:
        # Create-ahead: current week + next two (3 partitions, ~2-week window).
        for stmt in _coding_agent_activity_partition_ddl(now):
            await conn.execute(
                text(
                    stmt
                )  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
            )

        # Drop: partitions whose week is more than 4 weeks before the current
        # week. Enumerate via `pg_inherits` (children of the parent), parse the
        # `pYYYYWW` suffix, drop those below the cutoff. `pg_inherits` join
        # avoids `LIKE`-based name scraping and only returns actual children.
        cutoff_lower = week_start - timedelta(weeks=4)
        cutoff_iso_year, cutoff_iso_week, _ = cutoff_lower.isocalendar()
        cutoff_key = cutoff_iso_year * 100 + cutoff_iso_week

        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_inherits i "
                    "JOIN pg_class c ON c.oid = i.inhrelid "
                    "JOIN pg_class p ON p.oid = i.inhparent "
                    "WHERE p.relname = 'coding_agent_activity'"
                )
            )
        ).all()
        for (name,) in rows:
            # Expected shape: coding_agent_activity_pYYYYWW
            suffix = name.removeprefix("coding_agent_activity_p")
            if suffix == name or len(suffix) != 6 or not suffix.isdigit():
                continue
            key = int(suffix)
            if key >= cutoff_key:
                continue
            # Identifier is a child of `coding_agent_activity` validated to match
            # the `pYYYYWW` shape — no caller data, injection-free.
            await conn.execute(
                text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                    f"DROP TABLE IF EXISTS {name}"
                )
            )


async def truncate_all_tables(session) -> None:
    """Empty every table in ``Base.metadata`` in reverse-FK order.

    Issues per-table ``DELETE FROM`` statements instead of a single
    ``TRUNCATE … CASCADE``. DELETE takes only ``RowExclusive`` locks, so
    it does not block on lingering ``AccessShare`` readers from a
    previous request — SSE subscribers, agent-gateway WebSockets, or
    background tasks still holding open transactions. TRUNCATE requires
    ``AccessExclusive`` on every table it touches and was causing the
    e2e reset endpoint to deadlock between specs.

    All yaaos primary keys are UUIDs, so no sequence-reset step is
    needed.

    Callers must ensure all model modules have been imported (so their
    tables are registered on ``Base.metadata``) before calling this. The
    ``app/testing/e2e_setup`` module handles that for the test reset path
    by importing every model module at the top of its file.
    """
    if not get_settings().is_non_prod:
        raise RuntimeError("truncate_all_tables is non-prod only")
    tables = list(reversed(Base.metadata.sorted_tables))
    if not tables:
        return
    # Set a short lock_timeout so that if a stuck connection is somehow
    # holding a conflicting row lock the reset fails fast with a clear
    # PostgresError instead of timing out at the HTTP layer.
    await session.execute(text("SET LOCAL lock_timeout = '2s'"))
    for table in tables:
        # `table.name` comes from SQLAlchemy model declarations in this
        # repo (resolved at import time before any request runs). No
        # request data feeds these names, so f-string interpolation is
        # injection-free. SQLAlchemy Core has no DELETE-without-where
        # builder for raw tables, so `text(...)` is the simplest fit.
        await session.execute(
            text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
                f'DELETE FROM "{table.name}"'
            )
        )


async def dispose() -> None:
    """Close the engine — used on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def shutdown() -> None:
    """Async alias for `dispose()`. Called by the process shutdown registries
    during web/worker teardown. Idempotent."""
    await dispose()
