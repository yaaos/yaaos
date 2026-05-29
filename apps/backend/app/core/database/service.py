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
# so re-running is safe. The `010_create_all_m02` migration adds identity + orgs +
# sessions tables and extends `audit_entries` with actor-kind columns.
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
    ("014_create_outbox_entries", "create_outbox_entries"),
    ("015_create_workflow_tables", "create_workflow_tables"),
    ("016_tickets_m05_columns", "tickets_m05_columns"),
    ("017_workspaces_m05_columns", "workspaces_m05_columns"),
    ("018_create_workspace_agents", "create_workspace_agents"),
    ("019_orgs_workspace_provider", "orgs_workspace_provider"),
    ("020_rename_member_to_builder", "rename_member_to_builder"),
    ("021_create_notifications", "create_notifications"),
    ("022_lessons_created_by", "lessons_created_by"),
    ("023_collapse_ticket_status", "collapse_ticket_status"),
    ("024_sso_email_domains", "sso_email_domains"),
    ("025_tickets_dedupe_external_id", "tickets_dedupe_external_id"),
    ("026_drop_github_poller_state", "drop_github_poller_state"),
    ("027_create_bearer_tokens", "create_bearer_tokens"),
    ("028_orgs_aws_region_and_arn_uniqueness", "orgs_aws_region_and_arn_uniqueness"),
    ("029_drop_github_installations", "drop_github_installations"),
    ("030_drop_github_settings", "drop_github_settings"),
    ("031_notifications_generalize_subject", "notifications_generalize_subject"),
    ("032_tickets_findings_rollup", "tickets_findings_rollup"),
    ("033_mcp_review_tokens_org_id", "mcp_review_tokens_org_id"),
    ("034_orgs_sso_authz_columns", "orgs_sso_authz_columns"),
    ("035_uuid_pk_uuidv7_defaults", "uuid_pk_uuidv7_defaults"),
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
        "app.domain.lessons.models",
        "app.domain.reviewer.models",
    ):
        importlib.import_module(mod)
    await conn.run_sync(Base.metadata.create_all)


async def _apply_add_github_settings_slug(conn) -> None:  # type: ignore[no-untyped-def]
    # No-op when `github_settings` is absent. There is no `github_settings`
    # model (migration 030 drops the table on legacy DBs; fresh DBs never
    # create it), so this column-add is meaningless and the bare ALTER would
    # 42P01 with `relation "github_settings" does not exist`. Skip cleanly
    # when the table isn't there.
    await conn.execute(
        text(
            "DO $$ BEGIN "
            "  IF EXISTS (SELECT 1 FROM information_schema.tables "
            "             WHERE table_name = 'github_settings') THEN "
            "    ALTER TABLE github_settings "
            "      ADD COLUMN IF NOT EXISTS slug TEXT NOT NULL DEFAULT ''; "
            "  END IF; "
            "END $$"
        )
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
    ]
    for stmt in statements:
        await conn.execute(text(stmt))

    # `github_poller_state` was retired in migration 026. Fresh DBs no longer
    # have the table at this point; legacy DBs that ran 001 with the old model
    # do — so all ALTERs against it are guarded.
    has_poller_state = await _table_exists(conn, "github_poller_state")
    if has_poller_state:
        await conn.execute(
            text(
                "ALTER TABLE github_poller_state ADD COLUMN IF NOT EXISTS"
                " repo_external_id TEXT NOT NULL DEFAULT ''"
            )
        )

    repos_exists = (await conn.execute(text("SELECT to_regclass('repos') IS NOT NULL"))).scalar()
    if repos_exists:
        backfills: list[str] = [
            "UPDATE lessons l SET plugin_id = r.plugin_id, repo_external_id = r.external_id"
            " FROM repos r WHERE l.repo_id = r.id",
            "UPDATE pull_requests p SET repo_external_id = r.external_id FROM repos r WHERE p.repo_id = r.id",
            "UPDATE tickets t SET plugin_id = r.plugin_id, repo_external_id = r.external_id"
            " FROM repos r WHERE t.repo_id = r.id",
        ]
        for stmt in backfills:
            await conn.execute(text(stmt))
        if has_poller_state:
            await conn.execute(
                text(
                    "UPDATE github_poller_state s SET repo_external_id = r.external_id"
                    " FROM repos r WHERE s.repo_id = r.id"
                )
            )

    drops: list[str] = [
        "ALTER TABLE lessons DROP COLUMN IF EXISTS repo_id",
        "ALTER TABLE pull_requests DROP COLUMN IF EXISTS repo_id",
        "ALTER TABLE tickets DROP COLUMN IF EXISTS repo_id",
        "CREATE INDEX IF NOT EXISTS lessons_repo_idx ON lessons (org_id, plugin_id, repo_external_id)",
        "DROP TABLE IF EXISTS repos",
    ]
    for stmt in drops:
        await conn.execute(text(stmt))
    if has_poller_state:
        for stmt in (
            "ALTER TABLE github_poller_state DROP CONSTRAINT IF EXISTS uq_github_poller_state_org_repo",
            "ALTER TABLE github_poller_state DROP COLUMN IF EXISTS repo_id",
            "ALTER TABLE github_poller_state"
            " ADD CONSTRAINT uq_github_poller_state_org_repo UNIQUE (org_id, repo_external_id)",
        ):
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
    `destination` so `run_review` callers can be distinguished by where their
    output goes.

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
    `review_jobs` (`kind`, `parent_comment_external_id`, `reply_body`) — the
    schema does not model replies.

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


async def _apply_create_all_identity(conn) -> None:  # type: ignore[no-untyped-def]
    """Identity + orgs + sessions migration.

    Adds: users, user_emails, oauth_identities, user_totp_secrets, orgs,
    memberships, invitations, sso_configs, sessions. The identity model also
    declared a `github_installations` table here; that table is dropped in
    migration 029, so it's no longer created on fresh DBs.
    Also extends `audit_entries` with `actor_user_id` + `actor_workspace_id`
    columns so the additive ActorKind values round-trip through the audit row.

    `create_all` is idempotent. The ALTERs on `audit_entries` use IF NOT
    EXISTS so re-runs against partially-migrated DBs are safe.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.core.identity.models")
    importlib.import_module("app.core.tenancy.models")
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


async def _apply_create_all_mcp(conn) -> None:  # type: ignore[no-untyped-def]
    """MCP context for coding agents.

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


async def _apply_create_outbox_entries(conn) -> None:  # type: ignore[no-untyped-def]
    """DB-atomic outbound message queue table.

     Backs `core/outbox.write()` + the drain loop in `apps/backend/app/worker.py`.
     Future phases add their own migrations as more tables come online
    . Idempotent.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.core.tasks.models")
    new_tables = [Base.metadata.tables["outbox_entries"]]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))


async def _apply_orgs_workspace_provider(conn) -> None:  # type: ignore[no-untyped-def]
    """per-org workspace provider selection columns. Idempotent."""
    statements: list[str] = [
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS workspace_provider TEXT",
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS registered_iam_arn TEXT",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_rename_member_to_builder(conn) -> None:  # type: ignore[no-untyped-def]
    """rename the `member` role to `builder`.

    `memberships.role` is a `TEXT` column (no enum type), so a row-level UPDATE
    is all the rename needs. Idempotent: re-running matches zero rows.
    """
    await conn.execute(text("UPDATE memberships SET role = 'builder' WHERE role = 'member'"))


async def _apply_create_notifications(conn) -> None:  # type: ignore[no-untyped-def]
    """create the `notifications` table + indexes.

    Idempotent: imports the model and runs `Base.metadata.create_all`
    which is `CREATE TABLE IF NOT EXISTS` underneath. The
    `app.core.notifications.models` import registers the table on
    `Base.metadata`.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.core.notifications.models")
    await conn.run_sync(Base.metadata.create_all)


async def _apply_lessons_created_by(conn) -> None:  # type: ignore[no-untyped-def]
    """add `lessons.created_by` (nullable UUID).

    Records the user who created the lesson when the SPA fired the
    request; nullable because pre-rows have no attribution and
    system-created lessons (workspace agent, reviewer) stay anonymous.
    """
    await conn.execute(text("ALTER TABLE lessons ADD COLUMN IF NOT EXISTS created_by UUID"))


async def _apply_collapse_ticket_status(conn) -> None:  # type: ignore[no-untyped-def]
    """collapse `tickets.status` to the 5-state vocab.

    Legacy lifecycle (open / in_review / complete / abandoned) is
    rewritten to the display vocab (running / hitl / done / failed
    / cancelled) one-shot. `hitl` and `failed` are reserved for the
    workflow-state projection to populate on later transitions; the
    static migration only maps the four legacy values. Idempotent:
    re-running matches zero rows.
    """
    statements: list[str] = [
        "UPDATE tickets SET status = 'running' WHERE status IN ('open', 'in_review')",
        "UPDATE tickets SET status = 'done' WHERE status = 'complete'",
        "UPDATE tickets SET status = 'cancelled' WHERE status = 'abandoned'",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_drop_github_poller_state(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop the `github_poller_state` table. The boot-time catch-up poller
    was retired — webhooks are sufficient for POC. The table existed only to
    track per-repo last-poll cursors. Idempotent."""
    await conn.execute(text("DROP TABLE IF EXISTS github_poller_state"))


async def _apply_create_bearer_tokens(conn) -> None:  # type: ignore[no-untyped-def]
    """bearer-token ledger table.

    Backs `/identity/exchange` issuance and the bearer verifier on every
    other gateway endpoint. `token_hash` is sha256 of the
    plaintext; plaintext is returned to the caller exactly once. Idempotent.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.core.agent_gateway.models")
    new_tables = [Base.metadata.tables["bearer_tokens"]]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))

    # Partial index for fast "active bearer for this agent" lookups. Declared
    # here rather than on the model — SQLAlchemy renders partial indexes
    # differently across dialects.
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_bearer_tokens_agent_active "
            "ON bearer_tokens (agent_id, expires_at) WHERE revoked_at IS NULL"
        )
    )


async def _apply_orgs_aws_region_and_arn_uniqueness(conn) -> None:  # type: ignore[no-untyped-def]
    """finalize org-level AWS-IAM config.

    Adds `orgs.aws_region` (the STS region the org's agent runs in — used to
    pin the signed-request endpoint and defend against cross-region replay).
    Adds a UNIQUE index on `orgs.registered_iam_arn` so the same role ARN
    can't be claimed by two orgs (a unique index — not a constraint — so
    NULLs continue to be allowed for orgs in in-memory mode). Adds a CHECK
    constraint enforcing that `registered_iam_arn` and `aws_region` are
    both-or-neither. Idempotent.
    """
    statements: list[str] = [
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS aws_region TEXT",
        # Pre-migration `registered_iam_arn` values landed in 019 without a
        # paired region (the column didn't exist yet). The new check constraint
        # requires both-or-neither, so clear any orphan ARNs before the check
        # is enforced. POC-safe: any test-configured ARN must be re-registered
        # through the new Workspace settings page.
        "UPDATE orgs SET registered_iam_arn = NULL "
        "WHERE registered_iam_arn IS NOT NULL AND aws_region IS NULL",
        # Postgres treats NULLs as distinct in unique indexes by default —
        # multiple in-memory orgs with NULL ARN don't collide.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orgs_registered_iam_arn "
        "ON orgs (registered_iam_arn) WHERE registered_iam_arn IS NOT NULL",
        "ALTER TABLE orgs DROP CONSTRAINT IF EXISTS ck_orgs_arn_region_paired",
        "ALTER TABLE orgs ADD CONSTRAINT ck_orgs_arn_region_paired "
        "CHECK ((registered_iam_arn IS NULL) = (aws_region IS NULL))",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_drop_github_installations(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop the legacy `github_installations` table.

    The richer `github_app_installations` (created via the github plugin's
    models) is now the single source of truth for install bindings —
    `account_login`, `status`, multi-install support, etc. The legacy table
    only carried `(installation_id, org_id, created_at)` and had no
    production readers after the install callback was migrated to write the
    plugin table directly.
    """
    await conn.execute(text("DROP TABLE IF EXISTS github_installations"))


async def _apply_drop_github_settings(conn) -> None:  # type: ignore[no-untyped-def]
    """Drop the per-org `github_settings` table.

    Replaced by a single platform GitHub App whose credentials live in env
    vars (`yaaos_github_app_*`). The per-org table only existed to support a
    "bring your own GitHub App" model that was always wrong-shaped for SaaS —
    customers click "Install yaaos" instead of registering their own App.
    `github_app_installations` keeps the per-org install_id binding; no other
    per-org github state survives.
    """
    await conn.execute(text("DROP TABLE IF EXISTS github_settings"))


async def _apply_tickets_dedupe_external_id(conn) -> None:  # type: ignore[no-untyped-def]
    """Add `(org_id, source, source_external_id)` UNIQUE on `tickets`.

    Pre-existing duplicates (produced by a pre-constraint race where two
    concurrent webhook deliveries both pass the existence check and both
    insert a fresh ticket row for the same PR) are deleted outright. The
    canonical row is the one a `pull_requests` row points at via `ticket_id`,
    falling back to the oldest by `created_at`. Audit rows for the deleted
    tickets are dropped first so the manual cleanup is self-contained.

    Hard delete (rather than cancel) is required because the UNIQUE index is
    unconditional; status doesn't enter the key. Losers from the race never
    had a `review_job.scheduled` entry, so dropping their audit rows loses
    nothing the canonical row doesn't also carry.

    Idempotent — re-running produces no additional changes since the UNIQUE
    constraint then blocks future duplicates.
    """
    # Both deletes operate over the same ranked-by-(org,source,external) set;
    # a single statement with a data-modifying CTE keeps the SQL fully
    # literal (no f-string interpolation → semgrep-friendly) and atomically
    # drops audit rows + ticket rows in one transaction.
    await conn.execute(
        text(
            """
            WITH ranked AS (
              SELECT t.id,
                     row_number() OVER (
                       PARTITION BY t.org_id, t.source, t.source_external_id
                       ORDER BY
                         (EXISTS (SELECT 1 FROM pull_requests p WHERE p.ticket_id = t.id))::int DESC,
                         t.created_at ASC
                     ) AS rn
                FROM tickets t
            ),
            losers AS (
              SELECT id FROM ranked WHERE rn > 1
            ),
            audit_del AS (
              DELETE FROM audit_entries
               WHERE entity_kind = 'ticket'
                 AND entity_id IN (SELECT id FROM losers)
              RETURNING 1
            )
            -- Delete the loser tickets last. The PR upsert keys on
            -- (plugin_id, external_id) so only one ticket per (org, ext_id)
            -- is referenced by `pull_requests.ticket_id`; that row wins the
            -- ranking. The FK is NO ACTION, so a violation here would be a
            -- loud, correct failure rather than silent data loss.
            DELETE FROM tickets WHERE id IN (SELECT id FROM losers)
            """
        )
    )
    # Unique index enforces one ticket per (org, source, external id).
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_tickets_org_source_external "
            "ON tickets (org_id, source, source_external_id)"
        )
    )


async def _apply_sso_email_domains(conn) -> None:  # type: ignore[no-untyped-def]
    """add `sso_configs.email_domains` JSONB column.

    Drives the `/api/sso/discover` lookup: when a user types
    `*@acme.com` on the Login page, we look up the matching SSO config
    and return its provider. Existing rows backfill to `[]` (no claims).
    Idempotent.
    """
    await conn.execute(
        text(
            "ALTER TABLE sso_configs ADD COLUMN IF NOT EXISTS email_domains JSONB NOT NULL DEFAULT '[]'::jsonb"
        )
    )


async def _apply_create_workspace_agents(conn) -> None:  # type: ignore[no-untyped-def]
    """`workspace_agents` table: per-pod identity rows.

    Each agent pod that successfully exchanges identity gets a row. The
    `(org_id, agent_pod_id)` uniqueness constraint dedups across re-exchange
    after a pod restart. Idempotent."""
    import importlib  # noqa: PLC0415

    importlib.import_module("app.core.agent_gateway.models")
    new_tables = [Base.metadata.tables["workspace_agents"]]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))


async def _apply_workspaces_dispatch_columns(conn) -> None:  # type: ignore[no-untyped-def]
    """extend `workspaces` with the dispatch + claim
    columns. `provider` discriminates in-memory vs remote-agent;
    `current_command_id` + `current_holder_workflow_id` back the single-flight
    claim; `max_idle_seconds` feeds the idle-timeout sweep. Idempotent
    ALTERs; existing rows backfill to `provider='in_memory'`."""
    statements: list[str] = [
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'in_memory'",
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS current_command_id UUID",
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS current_holder_workflow_id UUID",
        "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS max_idle_seconds INTEGER NOT NULL DEFAULT 600",
        "CREATE INDEX IF NOT EXISTS ix_workspaces_current_holder_workflow_id "
        "ON workspaces (current_holder_workflow_id)",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_tickets_workflow_columns(conn) -> None:  # type: ignore[no-untyped-def]
    """extend `tickets` with `type`, `idempotency_key`,
    `payload`, and `current_workflow_execution_id`. Idempotent ALTERs;
    existing rows backfill `type='pr_review'` via the column default."""
    statements: list[str] = [
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'pr_review'",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS idempotency_key TEXT",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS current_workflow_execution_id UUID",
        # Sparse-unique: legacy rows with NULL idempotency_key don't collide.
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_tickets_idempotency_key ON tickets (idempotency_key) "
        "WHERE idempotency_key IS NOT NULL",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_create_workflow_tables(conn) -> None:  # type: ignore[no-untyped-def]
    """workflow engine tables.

    `workflow_executions` is the in-flight workflow state machine; one row
    per `core/workflow` execution. `pending_human_decisions` holds HITL
    pauses (one row per `awaiting_human` step). Idempotent.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.core.workflow.models")
    new_tables = [Base.metadata.tables[name] for name in ("workflow_executions", "pending_human_decisions")]
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=new_tables))


async def _apply_create_all_settings(conn) -> None:  # type: ignore[no-untyped-def]
    """settings + sidebar restructure.

    Adds: `users.github_username`, `orgs.session_timeout_override`,
    `orgs.vcs_plugin_id`, `orgs.vcs_settings`, `org_coding_agents`, `byok_keys`.
    All ALTERs use IF NOT EXISTS; `create_all` is idempotent. Safe to re-run.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("app.core.identity.models")
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


async def _apply_notifications_generalize_subject(conn) -> None:  # type: ignore[no-untyped-def]
    """Generalize `notifications.ticket_id` to a generic subject reference.

    - Rename `ticket_id → subject_id` (UUID, nullable).
    - Add `subject_type` (VARCHAR(64), nullable).
    - Backfill `subject_type = 'ticket'` for rows that had a non-null ticket_id.
    - Drop the old per-ticket-id index; add the new dedup index on
      `(user_id, type, subject_type, subject_id)` (partial: subject_type IS NOT NULL).
    Idempotent.
    """
    statements: list[str] = [
        # Rename column only if it still exists under the old name.
        "DO $$ BEGIN "
        "  IF EXISTS (SELECT 1 FROM information_schema.columns "
        "             WHERE table_name = 'notifications' AND column_name = 'ticket_id') THEN "
        "    ALTER TABLE notifications RENAME COLUMN ticket_id TO subject_id; "
        "  END IF; "
        "END $$",
        # Add subject_type if absent.
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS subject_type VARCHAR(64)",
        # Backfill: rows that had a ticket reference keep subject_type='ticket'.
        "UPDATE notifications SET subject_type = 'ticket' WHERE subject_id IS NOT NULL AND subject_type IS NULL",
        # Drop old single-column index (may not exist on fresh DBs).
        "DROP INDEX IF EXISTS notifications_ticket_id_idx",
        # Add dedup partial index.
        "CREATE UNIQUE INDEX IF NOT EXISTS notifications_dedup_subject_idx "
        "ON notifications (user_id, type, subject_type, subject_id) "
        "WHERE subject_type IS NOT NULL",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_tickets_findings_rollup(conn) -> None:  # type: ignore[no-untyped-def]
    """Denormalize findings rollup onto tickets.

    - Add `findings_count INT NOT NULL DEFAULT 0`.
    - Add `max_severity VARCHAR NULL`.
    - Backfill from existing findings via a grouped aggregate.
    Idempotent.
    """
    statements: list[str] = [
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS findings_count INT NOT NULL DEFAULT 0",
        "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS max_severity VARCHAR",
        # Backfill: for each ticket with a linked PR, compute rollup from findings.
        # severity_rank: high=3, medium=2, low=1, else=0.
        """
        UPDATE tickets t
        SET
            findings_count = COALESCE(agg.cnt, 0),
            max_severity = CASE agg.max_rank
                WHEN 3 THEN 'high'
                WHEN 2 THEN 'medium'
                WHEN 1 THEN 'low'
                ELSE NULL
            END
        FROM (
            SELECT
                f.pr_id,
                COUNT(f.id) AS cnt,
                MAX(
                    CASE f.severity
                        WHEN 'high'   THEN 3
                        WHEN 'medium' THEN 2
                        WHEN 'low'    THEN 1
                        ELSE 0
                    END
                ) AS max_rank
            FROM findings f
            GROUP BY f.pr_id
        ) agg
        WHERE t.pr_id = agg.pr_id
          AND t.findings_count = 0
        """,
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_mcp_review_tokens_org_id(conn) -> None:  # type: ignore[no-untyped-def]
    """Add `org_id` to `mcp_review_tokens`.

    The proxy reads tenancy from the token row directly; no back-lookup into
    the reviewer module is required. Tokens are short-lived (2h TTL) — any
    tokens that existed before this migration are likely already expired, so
    the NULL default is acceptable for the column add. The column is made
    NOT NULL via `SET NOT NULL` after backfilling from the joined reviews row
    for any still-live tokens.
    Idempotent.
    """
    statements: list[str] = [
        # Add nullable first so the ALTER succeeds on non-empty tables.
        "ALTER TABLE mcp_review_tokens ADD COLUMN IF NOT EXISTS org_id UUID",
        # Backfill from the reviews FK for any rows that predate this migration.
        "UPDATE mcp_review_tokens t SET org_id = r.org_id "
        "FROM reviews r WHERE r.id = t.review_id AND t.org_id IS NULL",
        # Now enforce NOT NULL.
        "ALTER TABLE mcp_review_tokens ALTER COLUMN org_id SET NOT NULL",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


async def _apply_uuid_pk_uuidv7_defaults(conn) -> None:  # type: ignore[no-untyped-def]
    """Set `DEFAULT uuidv7()` on every UUID primary-key column.

    All UUID PK columns now carry `server_default=text("uuidv7()")` on
    the SQLAlchemy model. Fresh DBs created via `create_all` already get
    the default. This migration backfills the default on existing tables.
    Idempotent: `ALTER COLUMN ... SET DEFAULT` on an already-defaulted
    column is a no-op.
    """
    # One literal per table. Non-literal text() args are rejected by
    # check_table_access; every SQL string is a plain literal.
    await conn.execute(text("ALTER TABLE users ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE user_emails ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE oauth_identities ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE orgs ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE audit_entries ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE workspaces ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE workspace_agents ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE bearer_tokens ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE outbox_entries ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE workflow_executions ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE pending_human_decisions ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE notifications ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE claude_code_settings ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE github_app_installations ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE github_webhook_events ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE tickets ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE pull_requests ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE invitations ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE lessons ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE reviews ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE findings ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE finding_observations ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE comment_threads ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE comment_messages ALTER COLUMN id SET DEFAULT uuidv7()"))
    await conn.execute(text("ALTER TABLE acknowledgment_decisions ALTER COLUMN id SET DEFAULT uuidv7()"))


async def _apply_orgs_sso_authz_columns(conn) -> None:  # type: ignore[no-untyped-def]
    """Add denormalized SSO authz columns to `orgs`.

    Adds `sso_enabled BOOL NOT NULL DEFAULT false` and
    `sso_exempt_owner_user_id UUID NULL` to the `orgs` table, then backfills
    from `sso_configs.enabled` / `sso_configs.exempt_owner_user_id` for orgs
    that already have an SSO config row. The source columns lack the `sso_`
    prefix — rename-on-copy, not a straight move.

    Idempotent: `ADD COLUMN IF NOT EXISTS` on repeated runs.
    """
    statements: list[str] = [
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS sso_enabled BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE orgs ADD COLUMN IF NOT EXISTS sso_exempt_owner_user_id UUID REFERENCES users(id) ON DELETE SET NULL",
        # Backfill from sso_configs for orgs that already have a config row.
        "UPDATE orgs o SET sso_enabled = s.enabled, "
        "sso_exempt_owner_user_id = s.exempt_owner_user_id "
        "FROM sso_configs s WHERE s.org_id = o.id",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))


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


async def migrate() -> None:
    """Apply any un-applied migrations. Idempotent and concurrency-safe.

    Asserts the engine is Postgres >= 18 before touching any DDL — a wrong
    engine fails loudly here rather than deep inside a migration.

    Serializes via a Postgres session-scoped advisory lock held on a dedicated
    connection that spans the whole call. Two processes starting at once (web
    + worker, or two web instances) both call `migrate()`; whichever acquires
    the lock first applies, the other blocks, then re-reads `applied` inside
    the lock and finds nothing to do.

    Session-scoped (not `pg_advisory_xact_lock`) because per-migration commits
    happen in separate transactions — the lock has to outlive each. Today
    there is no pooler in front of Postgres; this lock would break under
    PgBouncer transaction pooling (session affinity is lost between
    statements) so a pooler in this path would need to be bypassed.
    """
    async with get_engine().connect() as conn:
        result = await conn.execute(text("SHOW server_version_num"))
        _assert_min_pg_version(result.scalar_one())
    await ensure_schema_migrations_table()
    async with get_engine().connect() as lock_conn:
        await lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY})
        try:
            await _apply_pending()
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY})


async def _apply_pending() -> None:
    """Body of `migrate()`, called while holding the advisory lock.

    Re-reads `applied` *inside* the lock so a follower that waited on a leader
    sees the freshly-applied rows and exits without doing anything.
    """
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
                await _apply_create_all_identity(conn)
            elif kind == "drop_claude_code_default_timeout_seconds":
                await _apply_drop_claude_code_default_timeout_seconds(conn)
            elif kind == "create_all_m03":
                await _apply_create_all_settings(conn)
            elif kind == "create_all_m04":
                await _apply_create_all_mcp(conn)
            elif kind == "create_outbox_entries":
                await _apply_create_outbox_entries(conn)
            elif kind == "create_workflow_tables":
                await _apply_create_workflow_tables(conn)
            elif kind == "tickets_m05_columns":
                await _apply_tickets_workflow_columns(conn)
            elif kind == "workspaces_m05_columns":
                await _apply_workspaces_dispatch_columns(conn)
            elif kind == "create_workspace_agents":
                await _apply_create_workspace_agents(conn)
            elif kind == "orgs_workspace_provider":
                await _apply_orgs_workspace_provider(conn)
            elif kind == "rename_member_to_builder":
                await _apply_rename_member_to_builder(conn)
            elif kind == "create_notifications":
                await _apply_create_notifications(conn)
            elif kind == "lessons_created_by":
                await _apply_lessons_created_by(conn)
            elif kind == "collapse_ticket_status":
                await _apply_collapse_ticket_status(conn)
            elif kind == "sso_email_domains":
                await _apply_sso_email_domains(conn)
            elif kind == "tickets_dedupe_external_id":
                await _apply_tickets_dedupe_external_id(conn)
            elif kind == "drop_github_poller_state":
                await _apply_drop_github_poller_state(conn)
            elif kind == "create_bearer_tokens":
                await _apply_create_bearer_tokens(conn)
            elif kind == "orgs_aws_region_and_arn_uniqueness":
                await _apply_orgs_aws_region_and_arn_uniqueness(conn)
            elif kind == "drop_github_installations":
                await _apply_drop_github_installations(conn)
            elif kind == "drop_github_settings":
                await _apply_drop_github_settings(conn)
            elif kind == "notifications_generalize_subject":
                await _apply_notifications_generalize_subject(conn)
            elif kind == "tickets_findings_rollup":
                await _apply_tickets_findings_rollup(conn)
            elif kind == "mcp_review_tokens_org_id":
                await _apply_mcp_review_tokens_org_id(conn)
            elif kind == "orgs_sso_authz_columns":
                await _apply_orgs_sso_authz_columns(conn)
            elif kind == "uuid_pk_uuidv7_defaults":
                await _apply_uuid_pk_uuidv7_defaults(conn)
            await conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version},
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
