"""SQLAlchemy async engine + session factory + `schema_migrations` bootstrap.

The skeleton uses no ORM models yet. The infrastructure exists so /health can
do a `SELECT 1` against the DB and so future modules drop in their tables
without reshaping bootstrap.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
        # In dev/test we use NullPool — avoids cross-event-loop contamination
        # in TestClient-driven integration tests where each test brings up a
        # fresh loop. Prod uses the default pool.
        if settings.yaaos_env == "dev":
            from sqlalchemy.pool import NullPool  # noqa: PLC0415

            kwargs["poolclass"] = NullPool
            kwargs.pop("pool_pre_ping", None)
        _engine = create_async_engine(settings.database_url, **kwargs)  # type: ignore[arg-type]
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
    """Yield an async session. Caller decides commit/rollback boundaries."""
    async with get_sessionmaker()() as s:
        yield s


async def ping() -> bool:
    """`SELECT 1` against the DB. Returns True on success, False on any error.

    Used by `/api/health` to report DB connectivity. Swallows all exceptions
    intentionally — the endpoint reports a boolean, not a stack trace.
    """
    try:
        async with session() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def ensure_schema_migrations_table() -> None:
    """Idempotently create the `schema_migrations` tracking table."""
    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        )


# M01 ships a single named migration ("001_create_all_m01"). Subsequent schema
# changes add new versions and the runner skips already-applied ones. The
# create_all approach is idempotent (CREATE TABLE IF NOT EXISTS underneath) so
# re-running is safe.
_M01_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("001_create_all_m01", "create_all"),
    ("002_github_settings_slug", "add_github_settings_slug"),
    ("003_drop_repos_table", "drop_repos_table"),
    ("004_review_jobs_triggered_by_destination", "add_review_jobs_triggered_by_destination"),
    ("005_drop_reviewer_agents", "drop_reviewer_agents"),
    ("006_review_jobs_activity_log_model_effort", "add_review_jobs_activity_log_model_effort"),
    ("007_create_durable_findings_tables", "create_durable_findings_tables"),
)


async def _apply_create_all(conn) -> None:  # type: ignore[no-untyped-def]
    import importlib  # noqa: PLC0415

    for mod in (
        "app.core.audit_log.models",
        "app.core.workspace.models",
        "app.plugins.claude_code.models",
        "app.plugins.github.models",
        "app.domain.pull_requests.models",
        "app.domain.tickets.models",
        "app.domain.memory.models",
        "app.domain.reviewer.models",
    ):
        importlib.import_module(mod)
    await conn.run_sync(Base.metadata.create_all)


async def _apply_add_github_settings_slug(conn) -> None:  # type: ignore[no-untyped-def]
    # Idempotent ALTER — works on fresh DBs where `create_all` already added the
    # column from the model, and on existing DBs where 001 ran before the column
    # was added to the model.
    await conn.execute(
        text("ALTER TABLE github_settings ADD COLUMN IF NOT EXISTS slug TEXT NOT NULL DEFAULT ''")
    )


async def _apply_drop_repos_table(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop the `repos` table; convert dependents from FK(repo_id) → string(repo_external_id).

    Backfills `repo_external_id` from `repos.external_id` before the FK column
    goes away. Idempotent. One statement per execute (asyncpg doesn't accept
    multi-statement prepared statements).
    """
    statements: list[str] = [
        "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS plugin_id TEXT NOT NULL DEFAULT 'github'",
        "ALTER TABLE lessons ADD COLUMN IF NOT EXISTS repo_external_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE pull_requests ADD COLUMN IF NOT EXISTS repo_external_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS plugin_id TEXT NOT NULL DEFAULT 'github'",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS repo_external_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE github_poller_state ADD COLUMN IF NOT EXISTS repo_external_id TEXT NOT NULL DEFAULT ''",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))

    repos_exists = (await conn.execute(text("SELECT to_regclass('repos') IS NOT NULL"))).scalar()
    if repos_exists:
        backfills: list[str] = [
            "UPDATE lessons l SET plugin_id = r.plugin_id, repo_external_id = r.external_id"
            " FROM repos r WHERE l.repo_id = r.id",
            "UPDATE pull_requests p SET repo_external_id = r.external_id FROM repos r WHERE p.repo_id = r.id",
            "UPDATE tickets t SET plugin_id = r.plugin_id, repo_external_id = r.external_id"
            " FROM repos r WHERE t.repo_id = r.id",
            "UPDATE github_poller_state s SET repo_external_id = r.external_id"
            " FROM repos r WHERE s.repo_id = r.id",
        ]
        for stmt in backfills:
            await conn.execute(text(stmt))

    drops: list[str] = [
        "ALTER TABLE lessons DROP COLUMN IF EXISTS repo_id",
        "ALTER TABLE pull_requests DROP COLUMN IF EXISTS repo_id",
        "ALTER TABLE tickets DROP COLUMN IF EXISTS repo_id",
        "ALTER TABLE github_poller_state DROP CONSTRAINT IF EXISTS uq_github_poller_state_org_repo",
        "ALTER TABLE github_poller_state DROP COLUMN IF EXISTS repo_id",
        "ALTER TABLE github_poller_state"
        " ADD CONSTRAINT uq_github_poller_state_org_repo UNIQUE (org_id, repo_external_id)",
        "CREATE INDEX IF NOT EXISTS lessons_repo_idx ON lessons (org_id, plugin_id, repo_external_id)",
        "DROP TABLE IF EXISTS repos",
    ]
    for stmt in drops:
        await conn.execute(text(stmt))


async def _apply_add_review_jobs_triggered_by_destination(conn) -> None:  # type: ignore[no-untyped-def]
    """Promote audit-only `trigger_reason` into a queryable column and add
    `destination` so future `run_review` callers can be distinguished from
    today's `schedule_review → post-to-VCS` flow.

    Idempotent — works on fresh DBs (where `create_all` already added the
    columns from the model) and on existing DBs where this migration is the
    first time they appear.
    """
    statements: list[str] = [
        "ALTER TABLE review_jobs ADD COLUMN IF NOT EXISTS triggered_by TEXT NOT NULL DEFAULT 'pr_ready'",
        "ALTER TABLE review_jobs ADD COLUMN IF NOT EXISTS destination TEXT NOT NULL DEFAULT 'vcs'",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_add_review_jobs_activity_log_model_effort(conn) -> None:  # type: ignore[no-untyped-def]
    """Add `activity_log` (JSONB), `model`, `effort` columns to `review_jobs`;
    drop `cost_usd`. The activity log captures every Claude Code stream event
    in chronological order (cap 5 MB per row, enforced in app code). `model`
    + `effort` record what the CLI was asked to use (and what it reported on
    completion). Cost tracking is removed — the data we'd persist isn't
    authoritative pricing.

    Idempotent.
    """
    statements: list[str] = [
        "ALTER TABLE review_jobs ADD COLUMN IF NOT EXISTS activity_log JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE review_jobs ADD COLUMN IF NOT EXISTS model TEXT",
        "ALTER TABLE review_jobs ADD COLUMN IF NOT EXISTS effort TEXT",
        "ALTER TABLE review_jobs DROP COLUMN IF EXISTS cost_usd",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_drop_reviewer_agents(conn) -> None:  # type: ignore[no-untyped-def]
    """Collapse the per-agent decomposition: one row per (PR x review run).

    Drops `reviewer_agents` and the FKs that referenced it (`review_jobs.agent_id`,
    `posted_comments.agent_id`). Also drops the reply-related columns on
    `review_jobs` (`kind`, `parent_comment_external_id`, `reply_body`) — replies
    are deferred to a future `review_comments` table.

    Idempotent. CASCADE handles the FK references when dropping the parent table.
    """
    statements: list[str] = [
        # Indexes that reference soon-to-be-dropped columns.
        "ALTER TABLE review_jobs DROP COLUMN IF EXISTS agent_id",
        "ALTER TABLE review_jobs DROP COLUMN IF EXISTS kind",
        "ALTER TABLE review_jobs DROP COLUMN IF EXISTS parent_comment_external_id",
        "ALTER TABLE review_jobs DROP COLUMN IF EXISTS reply_body",
        "ALTER TABLE posted_comments DROP COLUMN IF EXISTS agent_id",
        "DROP TABLE IF EXISTS reviewer_agents CASCADE",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_create_durable_findings_tables(conn) -> None:  # type: ignore[no-untyped-def]
    """Create the durable-findings tables (plan §4.1).

    `findings`, `finding_observations`, `comment_threads`, `comment_messages`,
    `acknowledgment_decisions`. `create_all` is idempotent (CREATE TABLE IF NOT
    EXISTS) so fresh DBs that already ran 001 with these models in metadata
    no-op here, and DBs created before this migration's models existed get the
    tables added.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.domain.reviewer.models")
    new_tables = [
        Base.metadata.tables[name]
        for name in (
            "findings",
            "finding_observations",
            "comment_threads",
            "comment_messages",
            "acknowledgment_decisions",
        )
    ]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))


async def migrate() -> None:
    """Apply any un-applied migrations. Idempotent."""
    await ensure_schema_migrations_table()
    async with get_engine().begin() as conn:
        result = await conn.execute(text("SELECT version FROM schema_migrations"))
        applied = {row[0] for row in result}
    for version, kind in _M01_MIGRATIONS:
        if version in applied:
            continue
        async with get_engine().begin() as conn:
            if kind == "create_all":
                await _apply_create_all(conn)
            elif kind == "add_github_settings_slug":
                await _apply_add_github_settings_slug(conn)
            elif kind == "drop_repos_table":
                await _apply_drop_repos_table(conn)
            elif kind == "add_review_jobs_triggered_by_destination":
                await _apply_add_review_jobs_triggered_by_destination(conn)
            elif kind == "drop_reviewer_agents":
                await _apply_drop_reviewer_agents(conn)
            elif kind == "add_review_jobs_activity_log_model_effort":
                await _apply_add_review_jobs_activity_log_model_effort(conn)
            elif kind == "create_durable_findings_tables":
                await _apply_create_durable_findings_tables(conn)
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
            )


async def dispose() -> None:
    """Close the engine — used on shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
