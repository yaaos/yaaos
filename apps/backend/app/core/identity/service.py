"""Service entry-points for `core/identity`.

Re-exports public types and exposes the login orchestrator that providers
call from the OAuth callback. The orchestrator owns the identity-binding
rules; provider plugins only produce a normalized `ProviderProfile`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import repository as repo
from app.core.identity.models import OAuthIdentityRow, SessionRow, UserEmailRow, UserRow
from app.core.identity.providers import ProviderProfile
from app.core.identity.types import (
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
    "create_email",
    "create_oauth_identity",
    "create_session",
    "create_user",
    "login_via_oauth",
]


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of OAuth login orchestration.

    `user is None` means the OAuth profile is verified but no yaaos user
    matches it — neither by `(provider, external_subject)` nor by primary
    email. The caller redirects to `/login?reason=not_provisioned` with no
    cookie set; the user must be invited (by email) before they can sign
    in. This rule prevents stale cookies + DB wipes from spawning orphan
    accounts that infinite-bounce post-login.
    """

    user: User | None
    newly_created: bool


async def login_via_oauth(
    db: AsyncSession,
    *,
    provider_id: str,
    profile: ProviderProfile,
) -> LoginResult:
    """Apply the two-rule policy for an OAuth profile:

      1. (provider, external_subject) already bound → load that user.
      2. Verified email matches an existing user → auto-link: insert
         oauth_identities, return that user.
      3. No match → return `LoginResult(user=None, ...)`. The caller
         redirects to `/login?reason=not_provisioned`. Provisioning is
         invitation-only; no auto-create.

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

    return LoginResult(user=None, newly_created=False)


async def create_user(db: AsyncSession, *, display_name: str = "") -> UserRow:
    """Insert a new user row and return it. The caller owns the transaction."""
    return await repo.insert_user(db, display_name=display_name)


async def create_email(
    db: AsyncSession,
    *,
    user_id: UUID,
    email: str,
    is_primary: bool = False,
    verified: bool = False,
) -> UserEmailRow:
    """Insert an email row for `user_id` and return it. The caller owns the transaction."""
    return await repo.add_email(db, user_id=user_id, email=email, is_primary=is_primary, verified=verified)


async def create_oauth_identity(
    db: AsyncSession,
    *,
    user_id: UUID,
    provider: str,
    external_subject: str,
    verified: bool = True,
) -> OAuthIdentityRow:
    """Insert an oauth_identity row for `user_id` and return it. The caller owns the transaction."""
    return await repo.add_oauth_identity(
        db, user_id=user_id, provider=provider, external_subject=external_subject, verified=verified
    )


async def create_session(
    db: AsyncSession,
    *,
    token_hash: str,
    user_id: UUID | None,
    workspace_id: UUID | None,
    csrf_token: str,
    ip: str | None,
    user_agent: str | None,
    expires_at: datetime,
) -> SessionRow:
    """Insert a session row and return it. The caller owns the transaction."""
    return await repo.insert_session(
        db,
        token_hash=token_hash,
        user_id=user_id,
        workspace_id=workspace_id,
        csrf_token=csrf_token,
        ip=ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )


async def _set_session_last_seen_for_tests(
    db: AsyncSession,
    *,
    token_hash: str,
    last_seen_at: datetime,
) -> None:
    """Write `last_seen_at` for a session row identified by `token_hash`.
    Test-only helper to simulate idle sessions without importing `SessionRow`."""
    row = await repo.get_session_by_hash(db, token_hash)
    assert row is not None, f"session not found for hash: {token_hash[:8]}..."
    row.last_seen_at = last_seen_at
    await db.flush()


async def _delete_user_artifacts_for_tests(db: AsyncSession, *, user_id: UUID) -> None:
    """Delete all identity-owned rows for `user_id` (user, emails, OAuth
    identities, sessions). DB-level CASCADE handles child rows when deleting
    the user row via SQL DELETE — callers that need cross-module cleanup
    (e.g. memberships) must handle those separately. Test-only helper."""
    await db.execute(delete(UserRow).where(UserRow.id == user_id))
