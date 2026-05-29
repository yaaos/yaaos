"""Raw row access for `core/identity`.

Service-layer code uses these helpers; HTTP handlers never call repository
functions directly. Every function takes an `AsyncSession`; transaction
boundaries belong to the caller.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity.models import (
    OAuthIdentityRow,
    SessionRow,
    UserEmailRow,
    UserRow,
    UserTotpSecretRow,
)


def hash_token(raw_token: str) -> str:
    """SHA-256 hex of a raw session/invitation token. The DB only ever sees this."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def insert_user(session: AsyncSession, *, display_name: str = "") -> UserRow:
    row = UserRow(display_name=display_name)
    session.add(row)
    await session.flush()
    return row


async def get_user(session: AsyncSession, user_id: UUID) -> UserRow | None:
    return (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one_or_none()


async def set_user_display_name(session: AsyncSession, *, user_id: UUID, display_name: str) -> UserRow | None:
    row = await get_user(session, user_id)
    if row is None:
        return None
    row.display_name = display_name
    await session.flush()
    return row


async def set_user_github_username(
    session: AsyncSession, *, user_id: UUID, github_username: str | None
) -> UserRow | None:
    """Write `users.github_username`. Called by:
    - the github OAuth login callback on every successful login (keeps the
      column fresh if the user renames on GitHub)
    - the verify-only flow in `domain/account` (one-shot consent OAuth that
      writes only this column without issuing a session)."""
    row = await get_user(session, user_id)
    if row is None:
        return None
    row.github_username = github_username
    await session.flush()
    return row


async def add_email(
    session: AsyncSession,
    *,
    user_id: UUID,
    email: str,
    is_primary: bool = False,
    verified: bool = False,
) -> UserEmailRow:
    row = UserEmailRow(
        user_id=user_id,
        email=email,
        is_primary=is_primary,
        verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(row)
    await session.flush()
    return row


async def count_verified_emails(session: AsyncSession, user_id: UUID) -> int:
    """Number of verified emails the user owns. Used to enforce
    'removing the last verified email is blocked'."""
    from sqlalchemy import func as _func  # noqa: PLC0415

    stmt = (
        select(_func.count())
        .select_from(UserEmailRow)
        .where(
            UserEmailRow.user_id == user_id,
            UserEmailRow.verified_at.is_not(None),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def delete_email(session: AsyncSession, *, user_id: UUID, email_id: UUID) -> bool:
    """Delete one of the user's email rows. Returns False if the row
    doesn't exist or belongs to another user. Callers enforce the
    last-verified-email invariant before calling."""
    from sqlalchemy import delete as _sql_delete  # noqa: PLC0415

    result = await session.execute(
        _sql_delete(UserEmailRow)
        .where(UserEmailRow.id == email_id, UserEmailRow.user_id == user_id)
        .returning(UserEmailRow.id)
    )
    return result.first() is not None


async def find_user_by_email(session: AsyncSession, email: str) -> UserRow | None:
    """Lookup by any verified email. Returns None if no match or only matches
    are on deactivated users (lazy-reuse rule)."""
    stmt = (
        select(UserRow)
        .join(UserEmailRow, UserEmailRow.user_id == UserRow.id)
        .where(
            func.lower(UserEmailRow.email) == email.lower(),
            UserEmailRow.verified_at.is_not(None),
            UserRow.deactivated_at.is_(None),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_emails_for_user(session: AsyncSession, user_id: UUID) -> list[UserEmailRow]:
    stmt = select(UserEmailRow).where(UserEmailRow.user_id == user_id)
    return list((await session.execute(stmt)).scalars().all())


async def add_oauth_identity(
    session: AsyncSession,
    *,
    user_id: UUID,
    provider: str,
    external_subject: str,
    verified: bool = True,
) -> OAuthIdentityRow:
    row = OAuthIdentityRow(
        user_id=user_id,
        provider=provider,
        external_subject=external_subject,
        verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(row)
    await session.flush()
    return row


async def find_oauth_identity(
    session: AsyncSession, *, provider: str, external_subject: str
) -> OAuthIdentityRow | None:
    stmt = select(OAuthIdentityRow).where(
        OAuthIdentityRow.provider == provider,
        OAuthIdentityRow.external_subject == external_subject,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_session(
    session: AsyncSession,
    *,
    token_hash: str,
    user_id: UUID | None,
    workspace_id: UUID | None,
    csrf_token: str,
    ip: str | None,
    user_agent: str | None,
    expires_at: datetime,
) -> SessionRow:
    row = SessionRow(
        token_hash=token_hash,
        user_id=user_id,
        workspace_id=workspace_id,
        csrf_token=csrf_token,
        ip=ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_session_by_hash(session: AsyncSession, token_hash: str) -> SessionRow | None:
    return (
        await session.execute(select(SessionRow).where(SessionRow.token_hash == token_hash))
    ).scalar_one_or_none()


async def upsert_totp_secret(
    session: AsyncSession, *, user_id: UUID, encrypted_secret: bytes
) -> UserTotpSecretRow:
    existing = (
        await session.execute(select(UserTotpSecretRow).where(UserTotpSecretRow.user_id == user_id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.encrypted_secret = encrypted_secret
        existing.verified_at = None
        existing.last_used_at = None
        await session.flush()
        return existing
    row = UserTotpSecretRow(user_id=user_id, encrypted_secret=encrypted_secret)
    session.add(row)
    await session.flush()
    return row


async def get_totp_secret(session: AsyncSession, user_id: UUID) -> UserTotpSecretRow | None:
    return (
        await session.execute(select(UserTotpSecretRow).where(UserTotpSecretRow.user_id == user_id))
    ).scalar_one_or_none()
