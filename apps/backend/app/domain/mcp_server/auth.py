"""MCP bearer token lifecycle for `domain/mcp_server`.

`authenticate(bearer, *, session)` is the verification seam — it does a sha256
hash lookup in `mcp_access_tokens` and returns a `McpPrincipal` (user + org +
role) on success, or raises `McpAuthError` on failure.

Token mint / rotation helpers issue opaque bearers per the bearer-discipline
pattern: `token_urlsafe(32)` returned once; sha256-stored; raw never persists.

Access tokens: hours-scale TTL (`ACCESS_TOKEN_TTL`).
Refresh tokens: weeks-scale TTL (`REFRESH_TOKEN_TTL`); rotated on every use.

An hourly `@scheduled` task sweeps expired rows from both token tables and
prunes never-used client registrations older than `UNUSED_CLIENT_MAX_AGE`,
mirroring the `mcp_review_token_sweep` pattern.

User-deletion revocation: `revoke_tokens_for_user(user_id, session)` deletes
all token rows for a user — called from the same seam that deletes sessions.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from pydantic import BaseModel, SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Role
from app.core.database import session as db_session
from app.core.tasks import scheduled
from app.core.tenancy import get_member_role
from app.domain.mcp_server.models import (
    McpAccessTokenRow,
    McpAuthCodeRow,
    McpOAuthClientRow,
    McpRefreshTokenRow,
)

log = structlog.get_logger("domain.mcp_server")

# Token TTLs. Access tokens are hours-scale; refresh tokens weeks-scale.
ACCESS_TOKEN_TTL = timedelta(hours=8)
REFRESH_TOKEN_TTL = timedelta(weeks=4)

# A dynamic-client registration that never issued a code or token is pruned by
# the sweep once it is this old — /register is unauthenticated, so abandoned
# rows accumulate.
UNUSED_CLIENT_MAX_AGE = timedelta(days=7)


class McpAuthError(Exception):
    """Raised by `authenticate` when the bearer is missing, invalid, or expired.

    Maps to JSON-RPC -32004 / HTTP 401 at the tool-call boundary.
    """


class McpPrincipal(BaseModel, frozen=True):
    """Resolved identity behind an inbound MCP bearer: user + org + role.

    `org_id` is fixed at consent time.  `role` is resolved live from the
    membership table at `authenticate` time so a freshly demoted member is
    denied immediately without waiting for token rotation.
    """

    user_id: UUID
    org_id: UUID
    role: Role


def _hash_token(raw: str) -> str:
    """SHA-256 hex of a raw token. The DB stores only the hash."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _verify_pkce_s256(code_verifier: str, code_challenge: str) -> bool:
    """Return True iff `code_verifier` satisfies the stored S256 `code_challenge`.

    RFC 7636 §4.6: challenge = BASE64URL(SHA256(ASCII(verifier))).
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return computed == code_challenge


async def authenticate(
    bearer: SecretStr,
    *,
    session: AsyncSession,
) -> McpPrincipal:
    """Verify an inbound MCP bearer and return the resolved principal.

    Raises `McpAuthError` on any failure (missing token, wrong hash, expired,
    no live membership, etc.).  The caller maps this to JSON-RPC -32004 / HTTP 401.

    Role is resolved live from the membership table each call so that a
    freshly-demoted member is denied without waiting for token rotation.
    The org_id is locked to the consent-time selection and never changes.
    """
    raw = bearer.get_secret_value().strip()
    if not raw:
        raise McpAuthError("missing bearer")

    token_hash = _hash_token(raw)
    row = (
        await session.execute(select(McpAccessTokenRow).where(McpAccessTokenRow.token_hash == token_hash))
    ).scalar_one_or_none()

    if row is None:
        raise McpAuthError("invalid bearer")
    if row.expires_at < datetime.now(UTC):
        raise McpAuthError("bearer expired")

    # Resolve role live from the membership table; membership may have changed
    # since the token was issued.
    role = await get_member_role(session, org_id=row.org_id, user_id=row.user_id)
    if role is None:
        raise McpAuthError("no membership for token org")

    return McpPrincipal(user_id=row.user_id, org_id=row.org_id, role=role)


async def mint_access_token(
    *,
    client_id: UUID,
    user_id: UUID,
    org_id: UUID,
    session: AsyncSession,
) -> str:
    """Issue a fresh access token.  Returns the raw token exactly once;
    the DB stores only the sha256 hash."""
    raw = secrets.token_urlsafe(32)
    row = McpAccessTokenRow(
        token_hash=_hash_token(raw),
        client_id=client_id,
        user_id=user_id,
        org_id=org_id,
        expires_at=datetime.now(UTC) + ACCESS_TOKEN_TTL,
    )
    session.add(row)
    await session.flush()
    return raw


async def mint_refresh_token(
    *,
    client_id: UUID,
    user_id: UUID,
    org_id: UUID,
    session: AsyncSession,
) -> str:
    """Issue a fresh refresh token.  Returns the raw token exactly once."""
    raw = secrets.token_urlsafe(32)
    row = McpRefreshTokenRow(
        token_hash=_hash_token(raw),
        client_id=client_id,
        user_id=user_id,
        org_id=org_id,
        expires_at=datetime.now(UTC) + REFRESH_TOKEN_TTL,
    )
    session.add(row)
    await session.flush()
    return raw


async def rotate_refresh_token(
    raw_refresh: str,
    *,
    client_id: UUID,
    session: AsyncSession,
) -> tuple[str, str] | None:
    """Consume an existing refresh token and issue a new token pair.

    Returns `(new_access_raw, new_refresh_raw)` or `None` if the token is
    invalid, expired, or was issued to a different client. All validation —
    including the `client_id` match — happens BEFORE the old row is deleted:
    RFC 6749 §5.2 requires that a failed validation not consume the token, so
    the legitimate holder's refresh token survives a mismatched attempt.
    """
    token_hash = _hash_token(raw_refresh)
    row = (
        await session.execute(select(McpRefreshTokenRow).where(McpRefreshTokenRow.token_hash == token_hash))
    ).scalar_one_or_none()

    if row is None:
        return None
    if row.expires_at < datetime.now(UTC):
        return None
    if row.client_id != client_id:
        return None

    # Delete the consumed refresh token (rotation: each token is single-use).
    await session.execute(delete(McpRefreshTokenRow).where(McpRefreshTokenRow.token_hash == token_hash))

    new_access = await mint_access_token(
        client_id=row.client_id,
        user_id=row.user_id,
        org_id=row.org_id,
        session=session,
    )
    new_refresh = await mint_refresh_token(
        client_id=row.client_id,
        user_id=row.user_id,
        org_id=row.org_id,
        session=session,
    )
    return new_access, new_refresh


async def revoke_tokens_for_user(
    user_id: UUID,
    *,
    session: AsyncSession,
) -> None:
    """Delete all MCP token rows for `user_id`.

    Called alongside session deletion when a user is removed from the system,
    so MCP bearers cannot outlive the user row.
    """
    await session.execute(delete(McpAccessTokenRow).where(McpAccessTokenRow.user_id == user_id))
    await session.execute(delete(McpRefreshTokenRow).where(McpRefreshTokenRow.user_id == user_id))


async def _sweep_expired_tokens_and_unused_clients() -> None:
    """One pass: drop expired access + refresh token rows, then prune stale
    dynamic-client registrations.

    A registration that never completed an authorize leaves an `mcp_oauth_clients`
    row behind — the endpoint is unauthenticated, so these accumulate. A client is
    pruned only when it is older than `UNUSED_CLIENT_MAX_AGE` AND no auth code,
    access token, or refresh token references it. A client with a live token is
    kept regardless of age.

    Token deletion runs first, so a client whose last token has expired is
    prunable in the same pass.
    """
    now = datetime.now(UTC)
    async with db_session() as s:
        r1 = await s.execute(delete(McpAccessTokenRow).where(McpAccessTokenRow.expires_at < now))
        r2 = await s.execute(delete(McpRefreshTokenRow).where(McpRefreshTokenRow.expires_at < now))

        has_access = (
            select(McpAccessTokenRow.token_hash)
            .where(McpAccessTokenRow.client_id == McpOAuthClientRow.client_id)
            .exists()
        )
        has_refresh = (
            select(McpRefreshTokenRow.token_hash)
            .where(McpRefreshTokenRow.client_id == McpOAuthClientRow.client_id)
            .exists()
        )
        has_code = (
            select(McpAuthCodeRow.code_hash)
            .where(McpAuthCodeRow.client_id == McpOAuthClientRow.client_id)
            .exists()
        )
        r3 = await s.execute(
            delete(McpOAuthClientRow).where(
                McpOAuthClientRow.created_at < now - UNUSED_CLIENT_MAX_AGE,
                ~has_access,
                ~has_refresh,
                ~has_code,
            )
        )
        await s.commit()

    removed = int(r1.rowcount or 0) + int(r2.rowcount or 0)
    if removed:
        log.debug("mcp_server.tokens.swept", removed=removed)
    pruned = int(r3.rowcount or 0)
    if pruned:
        log.debug("mcp_server.clients.pruned", pruned=pruned)


# Hourly sweep — cluster-safe via `core/tasks` per-tick claim.
mcp_server_token_sweep = scheduled(
    name="mcp_server_token_sweep",
    cron="0 * * * *",
    queue="default",
    max_retries=1,
)(_sweep_expired_tokens_and_unused_clients)
