"""Bearer-token ledger for the agent gateway.

Every successful `/api/v1/agent/identity` issues a bearer through `issue()`,
which generates 32 random bytes via `secrets.token_urlsafe`, stores the
sha256 hash in `bearer_tokens`, and returns the plaintext exactly once.
Plaintext is never persisted and never logged. Subsequent gateway calls
authenticate by hashing the incoming bearer and looking it up via
`verify()`.

`verify()` returns `None` for any failure mode (expired, revoked, never
existed) — no error variants, no oracle. Callers map `None` to HTTP 401.

Revocation:
- `revoke()` sets `revoked_at` on a single bearer.
- `revoke_all_for_agent()` revokes every active bearer for an agent pod.
  Used by the manual-rotate path and by failsafe-6 agent-loss recovery.
- `revoke_all_for_org()` revokes every active bearer for an org. Used by
  the Workspace settings disconnect / mode-switch actions.
- `revoke_all_for_arn()` revokes every active bearer whose `issued_iam_arn`
  matches the supplied ARN. Used by `patch_org_settings` when the registered
  ARN changes or is cleared — old-ARN bearers 401 on next call.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway.models import BearerTokenRow
from app.core.database import session as db_session

# 1h default — matches the IdentityExchangeResponse contract. The agent
# re-exchanges ~5 min before expiry so a healthy agent never sees a 401
# from an expired bearer under normal operation.
DEFAULT_TTL_SECONDS = 60 * 60

# Bearer plaintext length: secrets.token_urlsafe(32) → ~43 base64url chars.
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class BearerContext:
    """Resolved identity attached to an authenticated request."""

    bearer_id: UUID
    agent_id: UUID
    org_id: UUID


@dataclass(frozen=True)
class BearerRecord:
    """Ledger row projection, plaintext-free. Returned by `issue()` alongside
    the plaintext token, and by `list_for_org()` for the UI bearers table."""

    id: UUID
    org_id: UUID
    agent_id: UUID
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None
    last_seen_at: datetime | None
    source_ip: str | None
    issued_iam_arn: str | None


def _hash(token: str) -> bytes:
    """Hash a bearer for storage / lookup. SHA-256 is collision-resistant
    enough for randomly-generated 32-byte tokens; HMAC adds nothing here
    because there's no shared secret to bind."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def _to_record(row: BearerTokenRow) -> BearerRecord:
    return BearerRecord(
        id=row.id,
        org_id=row.org_id,
        agent_id=row.agent_id,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoked_reason=row.revoked_reason,
        last_seen_at=row.last_seen_at,
        source_ip=row.source_ip,
        issued_iam_arn=row.issued_iam_arn,
    )


async def issue(
    *,
    agent_id: UUID,
    org_id: UUID,
    session: AsyncSession,
    source_ip: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    issued_iam_arn: str | None = None,
) -> tuple[str, BearerRecord]:
    """Generate a new bearer, persist its hash, return `(plaintext, record)`.

    Plaintext is the ONLY place the secret appears — the caller hands it
    back in the `/api/v1/agent/identity` response and the agent stores it in
    memory. Never returned again.

    `issued_iam_arn` records the canonical IAM ARN verified at issuance for audit.

    Caller owns the transaction. The row is flushed but not committed.
    """
    plaintext = secrets.token_urlsafe(_TOKEN_BYTES)
    now = datetime.now(UTC)
    row = BearerTokenRow(
        org_id=org_id,
        agent_id=agent_id,
        token_hash=_hash(plaintext),
        issued_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        revoked_at=None,
        revoked_reason=None,
        last_seen_at=None,
        source_ip=source_ip,
        issued_iam_arn=issued_iam_arn,
    )
    session.add(row)
    await session.flush()
    return plaintext, _to_record(row)


_verify_override = None


def set_verify_override(callback) -> None:  # type: ignore[no-untyped-def]
    """Test hook: swap `bearers.verify` for a stub. Pass `None` to restore.

    Lets WS / starlette TestClient tests sidestep cross-event-loop DB
    issues without losing auth coverage — the bearer ledger has its own
    direct tests in `test_bearers.py`.
    """
    global _verify_override
    _verify_override = callback


async def verify(token: str) -> BearerContext | None:
    """Hash + look up a bearer; return identity context if valid.

    Returns `None` for every rejection — expired, revoked, never existed —
    so callers can't distinguish failure modes from the response. This
    closes the oracle that would otherwise leak whether a guessed token
    ever existed.

    Opens its own short-lived session — verify happens on every gateway
    request and shouldn't piggyback on the caller's transaction.
    Updates `last_seen_at` on success.
    """
    if _verify_override is not None:
        return await _verify_override(token)
    if not token:
        return None
    token_hash = _hash(token)
    now = datetime.now(UTC)
    async with db_session() as s:
        row = (
            await s.execute(select(BearerTokenRow).where(BearerTokenRow.token_hash == token_hash))
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.revoked_at is not None:
            return None
        if row.expires_at <= now:
            return None
        # Best-effort last_seen update. If it fails (e.g. row vanished mid-flight)
        # the verify still succeeded; we don't roll back the auth decision.
        await s.execute(update(BearerTokenRow).where(BearerTokenRow.id == row.id).values(last_seen_at=now))
        await s.commit()
        return BearerContext(bearer_id=row.id, agent_id=row.agent_id, org_id=row.org_id)


async def revoke(bearer_id: UUID, reason: str, *, session: AsyncSession) -> None:
    """Mark a single bearer revoked. Idempotent — re-revoking is a no-op.

    Caller owns the transaction.
    """
    now = datetime.now(UTC)
    await session.execute(
        update(BearerTokenRow)
        .where(BearerTokenRow.id == bearer_id, BearerTokenRow.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )


async def revoke_all_for_agent(agent_id: UUID, reason: str, *, session: AsyncSession) -> int:
    """Revoke every active bearer for an agent pod. Returns count revoked.

    Used by `manual_rotate` (admin clicked Rotate on the Workspace page)
    and by failsafe-6 (`agent_loss` — supervisor's heartbeat has gone
    stale beyond threshold). Caller owns the transaction.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        update(BearerTokenRow)
        .where(
            BearerTokenRow.agent_id == agent_id,
            BearerTokenRow.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason=reason)
        .returning(BearerTokenRow.id)
    )
    return len(result.all())


async def revoke_all_for_org(org_id: UUID, reason: str, *, session: AsyncSession) -> int:
    """Revoke every active bearer for an org. Returns count revoked.

    Used by Workspace settings actions: `arn_change`, `mode_switch`,
    `disconnect`. Caller owns the transaction.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        update(BearerTokenRow)
        .where(
            BearerTokenRow.org_id == org_id,
            BearerTokenRow.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason=reason)
        .returning(BearerTokenRow.id)
    )
    return len(result.all())


async def revoke_all_for_arn(arn: str, reason: str, *, session: AsyncSession) -> int:
    """Revoke every active bearer whose `issued_iam_arn` matches `arn`. Returns count revoked.

    Used by `patch_org_settings` when the registered ARN changes or is cleared
    so agents that authenticated under the old ARN 401 on their next call.
    Caller owns the transaction.
    """
    now = datetime.now(UTC)
    result = await session.execute(
        update(BearerTokenRow)
        .where(
            BearerTokenRow.issued_iam_arn == arn,
            BearerTokenRow.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason=reason)
        .returning(BearerTokenRow.id)
    )
    return len(result.all())


async def list_for_org(org_id: UUID, *, limit: int = 50) -> list[BearerRecord]:
    """Recent bearers for the Workspace settings security feed / bearers table.

    Newest first by `issued_at`. Plaintext-free — `BearerRecord` carries
    only the row's metadata.
    """
    capped = max(1, min(limit, 500))
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(BearerTokenRow)
                    .where(BearerTokenRow.org_id == org_id)
                    .order_by(BearerTokenRow.issued_at.desc())
                    .limit(capped)
                )
            )
            .scalars()
            .all()
        )
        return [_to_record(r) for r in rows]


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "BearerContext",
    "BearerRecord",
    "issue",
    "list_for_org",
    "revoke",
    "revoke_all_for_agent",
    "revoke_all_for_arn",
    "revoke_all_for_org",
    "set_verify_override",
    "verify",
]
