"""Per-review MCP bearer lifecycle.

`mint_token(review_id, *, org_id) -> raw_token` issues a fresh bearer for a review:
32 URL-safe random bytes returned to the caller once, sha256-hashed and
persisted with `expires_at = created_at + 2h`. `lookup_token(raw)` reverses
the dance — returns the row if not expired, None otherwise. `revoke_token`
deletes by review_id (the caller invokes it at review-end). `sweep_expired`
drops anything past TTL (called hourly by the `@scheduled` worker task).

Raw tokens never persist. Lookups are constant-time-safe because the hash
is the primary key.

Required-session: every transactional function takes `session: AsyncSession`
from its caller; never commits. See `apps/backend/docs/patterns.md` §
Session management + atomicity.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import session as db_session
from app.core.tasks import scheduled
from app.domain.mcp_proxy.models import McpReviewTokenRow

log = structlog.get_logger("domain.mcp_proxy")


REVIEW_TOKEN_TTL = timedelta(hours=2)


class McpToken(BaseModel):
    """Value object returned by `lookup_token`. Represents a valid, non-expired bearer."""

    review_id: UUID
    org_id: UUID
    expires_at: datetime


def hash_token(raw: str) -> str:
    """SHA-256 hex of a raw MCP token. The DB stores only the hash."""
    return hashlib.sha256(raw.encode()).hexdigest()


async def mint_token(
    review_id: UUID,
    *,
    org_id: UUID,
    session: AsyncSession,
) -> str:
    """Issue a fresh bearer for a review. Returns the raw token exactly once;
    the DB sees only the sha256 hash. `org_id` is stored on the row so the
    proxy reads tenancy directly without a back-lookup into whatever module
    owns `review_id`."""
    raw = secrets.token_urlsafe(32)
    row = McpReviewTokenRow(
        token_hash=hash_token(raw),
        review_id=review_id,
        org_id=org_id,
        expires_at=datetime.now(UTC) + REVIEW_TOKEN_TTL,
    )
    session.add(row)
    await session.flush()
    return raw


async def lookup_token(
    raw_token: str,
    *,
    session: AsyncSession,
) -> McpToken | None:
    """Return a `McpToken` for `raw_token` if not expired; None otherwise.
    Raw tokens never live in the DB — we hash and look up by primary key."""
    token_hash = hash_token(raw_token)
    row = (
        await session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at < datetime.now(UTC):
        return None
    return McpToken(review_id=row.review_id, org_id=row.org_id, expires_at=row.expires_at)


async def get_token_by_hash(
    token_hash: str,
    *,
    session: AsyncSession,
) -> McpToken | None:
    """Return a `McpToken` value object for `token_hash`, or None if absent.
    Targeted read for tests that need to assert on the persisted token row
    after minting without importing the Row type directly."""
    row = (
        await session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None:
        return None
    return McpToken(review_id=row.review_id, org_id=row.org_id, expires_at=row.expires_at)


async def revoke_token(
    review_id: UUID,
    *,
    session: AsyncSession,
) -> int:
    """Drop every token row for a review. Returns the count removed (review
    teardown calls this before the workspace is destroyed)."""
    result = await session.execute(delete(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review_id))
    return int(result.rowcount or 0)


# `record_broken_creds` is called here (producer-side) by the MCP proxy
# dispatcher on every broken-creds / not-connected response. `consume_broken_creds`
# exists but has no production caller today — no consumer yet drains the
# tracker to prefix a warning onto review output at review-end. Observations
# accumulate in the dict and are cleared only by the in-process tests that
# call `consume_broken_creds` directly.
#
# Per-review tracker for broken_creds / not_connected observations, intended
# to be drained at review-end to prefix the PR comment with a yellow warning
# block listing affected providers. Process-local — reviews finish within
# minutes and a restart kills the in-flight review task anyway.
_broken_creds_observed: dict[UUID, set[str]] = {}


def record_broken_creds(review_id: UUID, provider: str) -> None:
    _broken_creds_observed.setdefault(review_id, set()).add(provider)


def consume_broken_creds(review_id: UUID) -> set[str]:
    """Return providers observed broken for `review_id` and clear the entry."""
    return _broken_creds_observed.pop(review_id, set())


async def sweep_expired(*, session: AsyncSession) -> int:
    """Periodic-cleanup helper. Drops rows past TTL; returns the count."""
    result = await session.execute(
        delete(McpReviewTokenRow).where(McpReviewTokenRow.expires_at < datetime.now(UTC))
    )
    return int(result.rowcount or 0)


async def _sweep_once() -> None:
    """One pass: drop expired `mcp_review_tokens` rows."""
    async with db_session() as s:
        n_swept = await sweep_expired(session=s)
        await s.commit()
    if n_swept:
        log.debug("mcp_proxy.tokens.swept", removed=n_swept)


# Hourly sweep — cluster-safe via `core/tasks` per-tick claim.
# Exactly one worker pod enqueues per slot. Body is idempotent.
mcp_review_token_sweep = scheduled(
    name="mcp_review_token_sweep",
    cron="0 * * * *",
    queue="default",
    max_retries=1,
)(_sweep_once)
