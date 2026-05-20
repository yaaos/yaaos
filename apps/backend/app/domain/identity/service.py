"""Service entry-points for `domain/identity`.

Re-exports public types and exposes the login orchestrator that providers
call from the OAuth callback. The orchestrator owns the matching / linking /
hard-reject rules; provider plugins only produce a normalized
`ProviderProfile`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity import repository as repo
from app.domain.identity.providers import ProviderProfile
from app.domain.identity.types import (
    EmailAlreadyLinkedError,
    HardRejectError,
    LinkChallengeRequiredError,
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
    "HardRejectError",
    "LinkChallengeRequiredError",
    "LoginResult",
    "OAuthIdentity",
    "Session",
    "SessionNotFoundError",
    "TotpError",
    "User",
    "UserEmail",
    "UserNotFoundError",
    "complete_oauth_link",
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
    """Apply matching / linking / hard-reject rules to an OAuth profile.

    Rules (in order):
      1. (provider, external_subject) hit → existing user → success.
      2. Email hit → existing user, provider not linked → raise
         LinkChallengeRequiredError. Caller drives the link-confirm flow.
      3. Pending invitation for email → create user + email + oauth identity,
         accept invitation, return success.
      4. Otherwise → HardRejectError.

    Unverified emails are rejected by the caller before this is invoked.
    """
    identity_row = await repo.find_oauth_identity(
        db, provider=provider_id, external_subject=profile.external_subject
    )
    if identity_row is not None:
        user_row = await repo.get_user(db, identity_row.user_id)
        assert user_row is not None
        # Keep `github_username` fresh on every GitHub login — handles the
        # case where the user renames their GitHub account.
        if provider_id == "github" and profile.provider_login:
            await repo.set_user_github_username(
                db, user_id=user_row.id, github_username=profile.provider_login
            )
        return LoginResult(user=User.from_row(user_row), newly_created=False)

    existing_user_row = await repo.find_user_by_email(db, profile.primary_email)
    if existing_user_row is not None:
        raise LinkChallengeRequiredError(f"{profile.primary_email}:{provider_id}:{profile.external_subject}")

    invitation_row = await _find_pending_invitation_by_email(db, profile.primary_email)
    if invitation_row is None:
        raise HardRejectError(profile.primary_email)

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
    await _accept_invitation_for_user(db, invitation_row, user_id=user_row.id)
    return LoginResult(user=User.from_row(user_row), newly_created=True)


async def complete_oauth_link(
    db: AsyncSession,
    *,
    user_id: UUID,
    provider_id: str,
    external_subject: str,
) -> OAuthIdentity:
    """Attach `(provider_id, external_subject)` to `user_id`. Used after the
    link-confirm flow. Emits a `provider_linked` audit row per membership
    org so each org sees the link event from a user-domain entity."""
    existing = await repo.find_oauth_identity(db, provider=provider_id, external_subject=external_subject)
    if existing is not None and existing.user_id != user_id:
        raise EmailAlreadyLinkedError(provider_id)
    if existing is not None:
        return OAuthIdentity.from_row(existing)
    row = await repo.add_oauth_identity(
        db,
        user_id=user_id,
        provider=provider_id,
        external_subject=external_subject,
    )
    await _emit_link_audit(db, user_id=user_id, provider_id=provider_id, kind="provider_linked")
    return OAuthIdentity.from_row(row)


class _LinkAuditPayload(BaseModel):
    provider: str


async def _emit_link_audit(db: AsyncSession, *, user_id: UUID, provider_id: str, kind: str) -> None:
    """Write one `provider_linked` (or `_unlinked`) audit row per membership
    org. Identity events are user-global; the audit table requires `org_id`
    so we fan out by membership. Users with no memberships emit nothing."""
    from app.core.audit_log import Actor, audit  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    memberships = await orgs_repo.list_memberships_for_user(db, user_id)
    actor = Actor.user(user_id=user_id)
    for m in memberships:
        await audit(
            "user",
            user_id,
            kind,
            _LinkAuditPayload(provider=provider_id),
            actor,
            org_id=m.org_id,
            session=db,
        )


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
    """Mark the invitation accepted and create the membership.

    Phase 6 fleshes out the full invite/accept service. This minimal path
    exists so first-login signup completes when an admin pre-invited the user.
    """
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
