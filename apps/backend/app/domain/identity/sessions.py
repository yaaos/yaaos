"""Session lifecycle for `domain/identity`.

Opaque server-side sessions. The raw token is 32 random bytes (URL-safe base64
for cookie shipping); the DB stores only `hashlib.sha256(raw).hexdigest()`.

Rotation = create new row + delete old in the same transaction. Revoke-all =
delete every row by `user_id`.

Phase 6 wires session rotation into role-change + invite-accept; Phase 12
extends `sso_satisfied_for_org_id` semantics. This module owns the table
and the lifecycle primitives.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.identity.models import SessionRow
from app.domain.identity.types import Session, SessionNotFoundError


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _new_raw_token() -> str:
    """32 random bytes; URL-safe base64. Caller sets this on a cookie."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class CreatedSession:
    """Result of `create`. The raw token is the cookie value; the CSRF token
    is what the SPA echoes in `X-CSRF-Token` on mutations. Both are returned
    only here — never read out of the DB again."""

    raw_token: str
    csrf_token: str
    session: Session


async def create(
    session: AsyncSession,
    *,
    user_id: UUID | None,
    workspace_id: UUID | None,
    ip: str | None = None,
    user_agent: str | None = None,
    lifetime: timedelta | None = None,
) -> CreatedSession:
    """Mint a new session row. Exactly one of `user_id` / `workspace_id` must
    be set; the other stays None. Workspace sessions are reserved for M03+
    but the column shape lands now to avoid a later migration."""
    if (user_id is None) == (workspace_id is None):
        raise ValueError("exactly one of user_id / workspace_id must be set")
    raw = _new_raw_token()
    csrf = secrets.token_urlsafe(32)
    settings_lifetime = (
        lifetime if lifetime is not None else timedelta(seconds=get_settings().yaaos_session_lifetime_seconds)
    )
    expires = _now() + settings_lifetime
    row = SessionRow(
        token_hash=_hash(raw),
        user_id=user_id,
        workspace_id=workspace_id,
        csrf_token=csrf,
        ip=ip,
        user_agent=user_agent,
        expires_at=expires,
    )
    session.add(row)
    await session.flush()
    return CreatedSession(raw_token=raw, csrf_token=csrf, session=Session.from_row(row))


async def lookup(session: AsyncSession, raw_token: str) -> Session | None:
    """Return the live session by raw token. None on missing/expired."""
    token_hash = _hash(raw_token)
    row = (
        await session.execute(select(SessionRow).where(SessionRow.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at < _now():
        return None
    return Session.from_row(row)


async def touch(session: AsyncSession, raw_token: str) -> None:
    """Update `last_seen_at` to now. No-op if the session doesn't exist or
    is expired."""
    token_hash = _hash(raw_token)
    await session.execute(
        update(SessionRow)
        .where(SessionRow.token_hash == token_hash, SessionRow.expires_at >= _now())
        .values(last_seen_at=_now())
    )


async def revoke(session: AsyncSession, raw_token: str) -> None:
    """Delete the row matching `raw_token`. No-op if absent."""
    token_hash = _hash(raw_token)
    await session.execute(sql_delete(SessionRow).where(SessionRow.token_hash == token_hash))


async def revoke_all_for_user(session: AsyncSession, user_id: UUID) -> int:
    """Delete every session for the user. Returns the count deleted."""
    result = await session.execute(
        sql_delete(SessionRow).where(SessionRow.user_id == user_id).returning(SessionRow.token_hash)
    )
    return len(result.all())


async def rotate(
    session: AsyncSession,
    old_raw_token: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> CreatedSession:
    """Atomically: create a new session for the same principal, delete the old
    row. Used on login (replacing pre-auth session), SSO satisfaction, role
    change. Raises `SessionNotFoundError` if `old_raw_token` doesn't resolve."""
    old = await lookup(session, old_raw_token)
    if old is None:
        raise SessionNotFoundError(old_raw_token[:8] + "…")
    new = await create(
        session,
        user_id=old.user_id,
        workspace_id=old.workspace_id,
        ip=ip,
        user_agent=user_agent,
    )
    await revoke(session, old_raw_token)
    return new


async def mark_sso_satisfied(session: AsyncSession, raw_token: str, *, org_id: UUID) -> Session:
    """Record that the session has satisfied SSO for `org_id` at this moment.
    The 8-hour TTL is enforced at read time in `is_sso_satisfied`."""
    token_hash = _hash(raw_token)
    now = _now()
    result = await session.execute(
        update(SessionRow)
        .where(SessionRow.token_hash == token_hash, SessionRow.expires_at >= now)
        .values(sso_satisfied_for_org_id=org_id, sso_satisfied_at=now)
        .returning(SessionRow)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise SessionNotFoundError(raw_token[:8] + "…")
    return Session.from_row(row)


SSO_TTL = timedelta(hours=8)


def is_sso_satisfied(s: Session, *, org_id: UUID) -> bool:
    """True iff the session has SSO-satisfied `org_id` within the last 8 hours."""
    if s.sso_satisfied_for_org_id != org_id:
        return False
    if s.sso_satisfied_at is None:
        return False
    return s.sso_satisfied_at + SSO_TTL >= _now()


async def cleanup_expired(session: AsyncSession) -> int:
    """Periodic-cleanup helper. Returns count of session rows purged."""
    result = await session.execute(
        sql_delete(SessionRow).where(SessionRow.expires_at < _now()).returning(SessionRow.token_hash)
    )
    return len(result.all())
