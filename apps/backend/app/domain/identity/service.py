"""Service entry-points for `domain/identity`.

Re-exports public types and exposes the login orchestrator that providers
call from the OAuth callback. The orchestrator owns the identity-binding
rules; provider plugins only produce a normalized `ProviderProfile`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity import repository as repo
from app.domain.identity.providers import ProviderProfile
from app.domain.identity.types import (
    EmailAlreadyLinkedError,
    OAuthIdentity,
    Session,
    SessionNotFoundError,
    TotpError,
    User,
    UserEmail,
    UserNotFoundError,
)

__all__ = [
    "EmailAlreadyLinkedError",
    "LoginResult",
    "OAuthIdentity",
    "Session",
    "SessionNotFoundError",
    "TotpError",
    "User",
    "UserEmail",
    "UserNotFoundError",
    "login_via_oauth",
]


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of a successful login orchestration. Caller turns this into a
    session row + cookie."""

    user: User
    newly_created: bool


async def login_via_oauth(
    db: AsyncSession,
    *,
    provider_id: str,
    profile: ProviderProfile,
) -> LoginResult:
    """Apply the three-rule policy for an OAuth profile:

      1. (provider, external_subject) already bound → load that user.
      2. Verified email matches an existing user, no identity row →
         auto-link: insert oauth_identities, return that user.
      3. No match → create a fresh user with the verified email + identity
         row. If a pending invitation exists for the email, accept it as
         part of user creation so the user lands in their invited org.

    Unverified emails are rejected by the caller before this is invoked.
    """
    identity_row = await repo.find_oauth_identity(
        db, provider=provider_id, external_subject=profile.external_subject
    )
    if identity_row is not None:
        user_row = await repo.get_user(db, identity_row.user_id)
        assert user_row is not None
        if provider_id == "github" and profile.provider_login:
            await repo.set_user_github_username(
                db, user_id=user_row.id, github_username=profile.provider_login
            )
        return LoginResult(user=User.from_row(user_row), newly_created=False)

    existing_user_row = await repo.find_user_by_email(db, profile.primary_email)
    if existing_user_row is not None:
        await repo.add_oauth_identity(
            db,
            user_id=existing_user_row.id,
            provider=provider_id,
            external_subject=profile.external_subject,
        )
        if provider_id == "github" and profile.provider_login:
            await repo.set_user_github_username(
                db, user_id=existing_user_row.id, github_username=profile.provider_login
            )
        return LoginResult(user=User.from_row(existing_user_row), newly_created=False)

    user_row = await repo.insert_user(db, display_name=profile.display_name)
    await repo.add_email(
        db,
        user_id=user_row.id,
        email=profile.primary_email,
        is_primary=True,
        verified=True,
    )
    await repo.add_oauth_identity(
        db,
        user_id=user_row.id,
        provider=provider_id,
        external_subject=profile.external_subject,
    )
    if provider_id == "github" and profile.provider_login:
        await repo.set_user_github_username(db, user_id=user_row.id, github_username=profile.provider_login)

    invitation_row = await _find_pending_invitation_by_email(db, profile.primary_email)
    if invitation_row is not None:
        await _accept_invitation_for_user(db, invitation_row, user_id=user_row.id)
    return LoginResult(user=User.from_row(user_row), newly_created=True)


async def _find_pending_invitation_by_email(db: AsyncSession, email: str):
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.domain.orgs.models import InvitationRow  # noqa: PLC0415

    now = datetime.now(UTC)
    stmt = (
        select(InvitationRow)
        .where(
            func.lower(InvitationRow.email) == email.lower(),
            InvitationRow.accepted_at.is_(None),
            InvitationRow.expires_at >= now,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _accept_invitation_for_user(db: AsyncSession, invitation_row, *, user_id: UUID) -> None:
    """Mark the invitation accepted and create the membership."""
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415
    from app.domain.orgs.types import Role  # noqa: PLC0415

    invitation_row.accepted_at = datetime.now(UTC)
    await orgs_repo.insert_membership(
        db,
        user_id=user_id,
        org_id=invitation_row.org_id,
        role=Role(invitation_row.role),
        handle=invitation_row.email.split("@")[0].lower()[:64],
    )
    await db.flush()
