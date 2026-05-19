"""Raw row access for `domain/identity`.

Service-layer code uses these helpers; HTTP handlers never call repository
functions directly. Every function takes an `AsyncSession`; transaction
boundaries belong to the caller.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity.models import (
    GithubInstallationRow,
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
    row = UserRow(id=uuid4(), display_name=display_name)
    session.add(row)
    await session.flush()
    return row


async def get_user(session: AsyncSession, user_id: UUID) -> UserRow | None:
    return (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one_or_none()


async def add_email(
    session: AsyncSession,
    *,
    user_id: UUID,
    email: str,
    is_primary: bool = False,
    verified: bool = False,
) -> UserEmailRow:
    row = UserEmailRow(
        id=uuid4(),
        user_id=user_id,
        email=email,
        is_primary=is_primary,
        verified_at=datetime.now(UTC) if verified else None,
    )
    session.add(row)
    await session.flush()
    return row


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
        id=uuid4(),
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


async def upsert_github_installation(
    session: AsyncSession, *, installation_id: int, org_id: UUID
) -> GithubInstallationRow:
    existing = (
        await session.execute(
            select(GithubInstallationRow).where(GithubInstallationRow.installation_id == installation_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.org_id = org_id
        await session.flush()
        return existing
    row = GithubInstallationRow(installation_id=installation_id, org_id=org_id)
    session.add(row)
    await session.flush()
    return row


async def find_installation_org(session: AsyncSession, installation_id: int) -> UUID | None:
    row = (
        await session.execute(
            select(GithubInstallationRow).where(GithubInstallationRow.installation_id == installation_id)
        )
    ).scalar_one_or_none()
    return row.org_id if row is not None else None
