"""SQLAlchemy async engine + session factory + `schema_migrations` bootstrap.

The skeleton uses no ORM models yet. The infrastructure exists so /health can
do a `SELECT 1` against the DB and so future modules drop in their tables
without reshaping bootstrap.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

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


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
        # In dev/test we use NullPool — avoids cross-event-loop contamination
        # in TestClient-driven integration tests where each test brings up a
        # fresh loop. Prod uses the default pool.
        if settings.is_non_prod:
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


# Migrations apply in declared order; the runner skips already-applied versions.
# The `create_all` kinds are idempotent (CREATE TABLE IF NOT EXISTS underneath)
# so re-running is safe. M02's `010_create_all_m02` adds identity + orgs +
# sessions tables and extends `audit_entries` with M02 actor-kind columns.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("001_create_all_m01", "create_all"),
    ("002_github_settings_slug", "add_github_settings_slug"),
    ("003_drop_repos_table", "drop_repos_table"),
    ("004_review_jobs_triggered_by_destination", "add_review_jobs_triggered_by_destination"),
    ("005_drop_reviewer_agents", "drop_reviewer_agents"),
    ("006_review_jobs_activity_log_model_effort", "add_review_jobs_activity_log_model_effort"),
    ("007_create_durable_findings_tables", "create_durable_findings_tables"),
    ("008_reviews_cutover", "reviews_cutover"),
    ("009_drop_classification_confidence", "drop_classification_confidence"),
    ("010_create_all_m02", "create_all_m02"),
    ("011_drop_claude_code_default_timeout_seconds", "drop_claude_code_default_timeout_seconds"),
    ("012_create_all_m03", "create_all_m03"),
    ("013_create_all_m04", "create_all_m04"),
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


async def _table_exists(conn, name: str) -> bool:  # type: ignore[no-untyped-def]
    """True iff a table with the given name lives in the current search_path.

    Used by the legacy `review_jobs` / `posted_comments` migrations: those
    tables existed in pre-cutover schemas but `001_create_all` now produces
    the post-cutover `reviews` table directly, so the ALTERs would target a
    table that was never created. Migration 008 (`reviews_cutover`) is the
    drop-and-recreate point.
    """
    result = await conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :n"),
        {"n": name},
    )
    return result.scalar() is not None


async def _apply_add_review_jobs_triggered_by_destination(conn) -> None:  # type: ignore[no-untyped-def]
    """Promote audit-only `trigger_reason` into a queryable column and add
    `destination` so future `run_review` callers can be distinguished from
    today's `schedule_review → post-to-VCS` flow.

    No-op on fresh DBs (post-cutover `001_create_all` produces `reviews`, not
    `review_jobs` — and `008_reviews_cutover` drops the old table anyway).
    On legacy DBs that ran 001 against the pre-cutover model, idempotently
    adds the two columns.
    """
    if not await _table_exists(conn, "review_jobs"):
        return
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

    No-op on fresh DBs — see `_apply_add_review_jobs_triggered_by_destination`.
    """
    if not await _table_exists(conn, "review_jobs"):
        return
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

    No-op on fresh DBs — neither `review_jobs` nor `posted_comments` exists
    post-cutover. Idempotent for legacy DBs.
    """
    has_review_jobs = await _table_exists(conn, "review_jobs")
    has_posted_comments = await _table_exists(conn, "posted_comments")
    statements: list[str] = []
    if has_review_jobs:
        statements.extend(
            [
                "ALTER TABLE review_jobs DROP COLUMN IF EXISTS agent_id",
                "ALTER TABLE review_jobs DROP COLUMN IF EXISTS kind",
                "ALTER TABLE review_jobs DROP COLUMN IF EXISTS parent_comment_external_id",
                "ALTER TABLE review_jobs DROP COLUMN IF EXISTS reply_body",
            ]
        )
    if has_posted_comments:
        statements.append("ALTER TABLE posted_comments DROP COLUMN IF EXISTS agent_id")
    # `reviewer_agents` itself uses IF EXISTS — safe regardless.
    statements.append("DROP TABLE IF EXISTS reviewer_agents CASCADE")
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


async def _apply_reviews_cutover(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop generation-1 review_jobs + posted_comments; create new `reviews` table.

    Per plan/notes/full-pr-flow.md §4.3 + §13 step 7 — drop-and-recreate. Existing
    POC data is throwaway. The new `reviews` table is created from the
    `ReviewRow` model via `Base.metadata.create_all`. After this runs, FindingRow
    + FindingObservationRow have a real FK target for review_id columns.
    """
    import importlib  # noqa: PLC0415

    statements = [
        # Drop generation-1 tables. CASCADE handles the posted_comments FK back
        # to review_jobs.
        "DROP TABLE IF EXISTS posted_comments CASCADE",
        "DROP TABLE IF EXISTS review_jobs CASCADE",
        # Drop the generation-2 findings tables too — they had unconstrained
        # UUID review_id columns; create_all will rebuild them with proper FKs
        # to reviews.id. Per drop-and-recreate, POC data is throwaway.
        "DROP TABLE IF EXISTS acknowledgment_decisions CASCADE",
        "DROP TABLE IF EXISTS comment_messages CASCADE",
        "DROP TABLE IF EXISTS comment_threads CASCADE",
        "DROP TABLE IF EXISTS finding_observations CASCADE",
        "DROP TABLE IF EXISTS findings CASCADE",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))

    # Re-register the reviewer module's models, then create the new tables.
    importlib.import_module("app.domain.reviewer.models")
    new_tables = [
        Base.metadata.tables[name]
        for name in (
            "reviews",
            "findings",
            "finding_observations",
            "comment_threads",
            "comment_messages",
            "acknowledgment_decisions",
        )
    ]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))


async def _apply_create_all_m02(conn) -> None:  # type: ignore[no-untyped-def]
    """M02 — identity + orgs + sessions.

    Adds: users, user_emails, oauth_identities, user_totp_secrets, orgs,
    memberships, invitations, sso_configs, sessions, github_installations.
    Also extends `audit_entries` with `actor_user_id` + `actor_workspace_id`
    columns so the additive ActorKind values round-trip through the audit row.

    `create_all` is idempotent. The ALTERs on `audit_entries` use IF NOT
    EXISTS so re-runs against partially-migrated DBs are safe.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.domain.identity.models")
    importlib.import_module("app.domain.orgs.models")
    new_tables = [
        Base.metadata.tables[name]
        for name in (
            "users",
            "user_emails",
            "oauth_identities",
            "user_totp_secrets",
            "orgs",
            "memberships",
            "invitations",
            "sso_configs",
            "sessions",
            "github_installations",
        )
    ]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))

    audit_alters = [
        "ALTER TABLE audit_entries ADD COLUMN IF NOT EXISTS actor_user_id UUID",
        "ALTER TABLE audit_entries ADD COLUMN IF NOT EXISTS actor_workspace_id UUID",
    ]
    for stmt in audit_alters:
        await conn.execute(text(stmt))

    # Partial unique indexes — Postgres-specific, declared here rather than
    # on the model since SQLAlchemy renders partial indexes differently across
    # dialects.
    partial_indexes = [
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_emails_email_active"
        " ON user_emails (lower(email))"
        " WHERE verified_at IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_invitations_pending_org_email"
        " ON invitations (org_id, lower(email))"
        " WHERE accepted_at IS NULL",
    ]
    for stmt in partial_indexes:
        await conn.execute(text(stmt))


async def _apply_drop_classification_confidence(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop `comment_messages.classification_confidence` and renormalize the
    legacy `acknowledgment` intent to `acknowledgment_clear`.

    Reasoning lives in `domain/reviewer/llm/classifier.py`: the LLM picks one
    of five categorical intents that encode the action directly, no separate
    probability axis. The old `acknowledgment` label always collapses to
    `acknowledgment_clear` (the act-immediately branch) — sub-threshold
    `acknowledgment` rows from the float-confidence era are vanishingly
    few at POC scale and don't justify a CASE WHEN reconstruction.
    """
    statements = [
        "ALTER TABLE comment_messages DROP COLUMN IF EXISTS classification_confidence",
        "UPDATE comment_messages SET classified_intent = 'acknowledgment_clear'"
        " WHERE classified_intent = 'acknowledgment'",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_create_all_m04(conn) -> None:  # type: ignore[no-untyped-def]
    """M04 — MCP context for coding agents.

    Adds `mcp_credentials` (per-(org, provider) OAuth tokens + allowlist) and
    `mcp_review_tokens` (per-review yaaos bearer for the proxy). `create_all`
    is idempotent. Safe to re-run.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.domain.integrations.models")
    importlib.import_module("app.domain.mcp_proxy.models")
    new_tables = [
        Base.metadata.tables[name]
        for name in (
            "mcp_credentials",
            "mcp_review_tokens",
        )
    ]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))


async def _apply_create_all_m03(conn) -> None:  # type: ignore[no-untyped-def]
    """M03 — settings + sidebar restructure.

    Adds: `users.github_username`, `orgs.session_timeout_override`,
    `orgs.vcs_plugin_id`, `orgs.vcs_settings`, `org_coding_agents`, `byok_keys`.
    All ALTERs use IF NOT EXISTS; `create_all` is idempotent. Safe to re-run.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.domain.identity.models")
    importlib.import_module("app.domain.orgs.models")
    importlib.import_module("app.core.byok.models")

    alters = [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username TEXT",
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS session_timeout_override INTEGER",
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS vcs_plugin_id TEXT",
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS vcs_settings JSONB",
    ]
    for stmt in alters:
        await conn.execute(text(stmt))

    new_tables = [
        Base.metadata.tables[name]
        for name in (
            "org_coding_agents",
            "byok_keys",
        )
    ]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))


async def _apply_drop_claude_code_default_timeout_seconds(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop the orphaned `claude_code_settings.default_timeout_seconds` column.

    The column was removed from `ClaudeCodeSettingsRow` in commit bfb929e
    (timeout default moved to code, fixed at 20s) but no migration shipped to
    drop the DB column. Fresh DBs created via `create_all` after that commit
    never had the column; older volumes still do, and it's `NOT NULL`, so any
    INSERT crashes with a NotNullViolationError. Idempotent.
    """
    await conn.execute(text("ALTER TABLE claude_code_settings DROP COLUMN IF EXISTS default_timeout_seconds"))


async def migrate() -> None:
    """Apply any un-applied migrations. Idempotent."""
    await ensure_schema_migrations_table()
    async with get_engine().begin() as conn:
        result = await conn.execute(text("SELECT version FROM schema_migrations"))
        applied = {row[0] for row in result}
    for version, kind in _MIGRATIONS:
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
            elif kind == "reviews_cutover":
                await _apply_reviews_cutover(conn)
            elif kind == "drop_classification_confidence":
                await _apply_drop_classification_confidence(conn)
            elif kind == "create_all_m02":
                await _apply_create_all_m02(conn)
            elif kind == "drop_claude_code_default_timeout_seconds":
                await _apply_drop_claude_code_default_timeout_seconds(conn)
            elif kind == "create_all_m03":
                await _apply_create_all_m03(conn)
            elif kind == "create_all_m04":
                await _apply_create_all_m04(conn)
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
