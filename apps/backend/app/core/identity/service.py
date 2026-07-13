"""Service entry-points for `core/identity`.

Re-exports public types and exposes the login orchestrator that providers
call from the OAuth callback. The orchestrator owns the identity-binding
rules; provider plugins only produce a normalized `ProviderProfile`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_ticket
from app.core.identity.models import UserRow
from app.core.identity.providers import ProviderProfile
from app.core.identity.repository import (
    add_email as _repo_add_email,
)
from app.core.identity.repository import (
    add_oauth_identity as _repo_add_oauth_identity,
)
from app.core.identity.repository import (
    find_oauth_identity as _repo_find_oauth_identity,
)
from app.core.identity.repository import (
    find_user_by_email as _repo_find_user_by_email,
)
from app.core.identity.repository import (
    get_session_by_hash,
    insert_session,
    insert_user,
)
from app.core.identity.repository import (
    get_user as _repo_get_user,
)
from app.core.identity.repository import (
    list_emails_for_user as _repo_list_emails_for_user,
)
from app.core.identity.repository import (
    set_user_display_name as _repo_set_user_display_name,
)
from app.core.identity.repository import (
    set_user_github_username as _repo_set_user_github_username,
)
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
from app.core.tenancy import list_active_member_ids

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
    "add_email",
    "change_display_name",
    "create_user",
    "find_oauth_identity",
    "find_user_by_email",
    "get_user",
    "link_oauth_identity",
    "list_emails_for_user",
    "login_via_oauth",
    "set_session_for_tests",
    "set_session_last_seen_for_tests",
    "update_github_handle",
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
    identity_row = await _repo_find_oauth_identity(
        db, provider=provider_id, external_subject=profile.external_subject
    )
    if identity_row is not None:
        user_row = await _repo_get_user(db, identity_row.user_id)
        assert user_row is not None
        if provider_id == "github" and profile.provider_login:
            await _repo_set_user_github_username(
                db, user_id=user_row.id, github_username=profile.provider_login
            )
        return LoginResult(user=User.from_row(user_row), newly_created=False)

    existing_user_row = await _repo_find_user_by_email(db, profile.primary_email)
    if existing_user_row is not None:
        await _repo_add_oauth_identity(
            db,
            user_id=existing_user_row.id,
            provider=provider_id,
            external_subject=profile.external_subject,
        )
        if provider_id == "github" and profile.provider_login:
            await _repo_set_user_github_username(
                db, user_id=existing_user_row.id, github_username=profile.provider_login
            )
        return LoginResult(user=User.from_row(existing_user_row), newly_created=False)

    return LoginResult(user=None, newly_created=False)


async def create_user(db: AsyncSession, *, display_name: str = "") -> User:
    """Insert a new user row and return it as a value object. The caller owns the transaction."""
    return User.from_row(await insert_user(db, display_name=display_name))


async def add_email(
    db: AsyncSession,
    *,
    user_id: UUID,
    email: str,
    is_primary: bool = False,
    verified: bool = False,
) -> UserEmail:
    """Insert an email row for `user_id` and return it as a value object. The caller owns the transaction."""
    return UserEmail.from_row(
        await _repo_add_email(db, user_id=user_id, email=email, is_primary=is_primary, verified=verified)
    )


async def link_oauth_identity(
    db: AsyncSession,
    *,
    user_id: UUID,
    provider: str,
    external_subject: str,
    verified: bool = True,
) -> OAuthIdentity:
    """Insert an oauth_identity row for `user_id` and return it as a value object. The caller owns the transaction."""
    return OAuthIdentity.from_row(
        await _repo_add_oauth_identity(
            db, user_id=user_id, provider=provider, external_subject=external_subject, verified=verified
        )
    )


async def get_user(db: AsyncSession, user_id: UUID) -> User | None:
    """Load a user by id and return it as a value object, or None if not found."""
    row = await _repo_get_user(db, user_id)
    return None if row is None else User.from_row(row)


async def find_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Lookup by any verified email and return it as a value object, or None if no active match."""
    row = await _repo_find_user_by_email(db, email)
    return None if row is None else User.from_row(row)


async def list_emails_for_user(db: AsyncSession, user_id: UUID) -> list[UserEmail]:
    """Return every email row for `user_id` as value objects."""
    rows = await _repo_list_emails_for_user(db, user_id)
    return [UserEmail.from_row(r) for r in rows]


async def find_oauth_identity(
    db: AsyncSession, *, provider: str, external_subject: str
) -> OAuthIdentity | None:
    """Lookup an oauth identity by (provider, external_subject) and return it as a value object, or None."""
    row = await _repo_find_oauth_identity(db, provider=provider, external_subject=external_subject)
    return None if row is None else OAuthIdentity.from_row(row)


async def change_display_name(db: AsyncSession, *, user_id: UUID, display_name: str) -> User | None:
    """Update the user's display_name; returns the resulting user as a value object, or None if not found."""
    row = await _repo_set_user_display_name(db, user_id=user_id, display_name=display_name)
    return None if row is None else User.from_row(row)


async def update_github_handle(
    db: AsyncSession, *, user_id: UUID, github_username: str | None
) -> User | None:
    """Write the user's github_username denorm; returns the resulting user as a value object, or None if not found."""
    row = await _repo_set_user_github_username(db, user_id=user_id, github_username=github_username)
    return None if row is None else User.from_row(row)


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
) -> Session:
    """Insert a session row and return it as a value object. The caller owns the transaction.

    Private to `core/identity` (not in `__all__`). Cross-module test seeds that need
    a deterministic token hash use `set_session_for_tests` instead.
    """
    return Session.from_row(
        await insert_session(
            db,
            token_hash=token_hash,
            user_id=user_id,
            workspace_id=workspace_id,
            csrf_token=csrf_token,
            ip=ip,
            user_agent=user_agent,
            expires_at=expires_at,
        )
    )


async def set_session_for_tests(
    db: AsyncSession,
    *,
    token_hash: str,
    user_id: UUID | None,
    workspace_id: UUID | None,
    csrf_token: str = "test-csrf",
    ip: str | None = None,
    user_agent: str | None = "test",
    expires_at: datetime,
) -> Session:
    """Test-only: insert a session row keyed by a caller-supplied ``token_hash``.

    Used by e2e seeds that need to set a known cookie value before driving a
    request through the auth chain. Production paths use `mint_session` (which
    generates a fresh random token internally) or `rotate_session`.
    """
    return await create_session(
        db,
        token_hash=token_hash,
        user_id=user_id,
        workspace_id=workspace_id,
        csrf_token=csrf_token,
        ip=ip,
        user_agent=user_agent,
        expires_at=expires_at,
    )


async def set_session_last_seen_for_tests(
    db: AsyncSession,
    *,
    token_hash: str,
    last_seen_at: datetime,
) -> None:
    """Write `last_seen_at` for a session row identified by `token_hash`.
    Test-only helper to simulate idle sessions without importing `SessionRow`."""
    row = await get_session_by_hash(db, token_hash)
    assert row is not None, f"session not found for hash: {token_hash[:8]}..."
    row.last_seen_at = last_seen_at
    await db.flush()


async def find_user_ids_by_github_username(github_username: str, *, session: AsyncSession) -> list[UUID]:
    """Return all non-deactivated user IDs whose ``github_username`` matches
    ``github_username`` (case-insensitive).

    Most callers expect exactly one result.  Zero means the PR author hasn't
    connected a GitHub account; two or more means a collision (same GitHub
    login on multiple accounts) — both cases should be treated as
    "unresolvable" by the caller.

    Callers that need to scope to a single org must intersect the returned
    IDs with ``core/tenancy.list_active_member_ids(session, org_id)``
    before deciding.
    """
    rows = (
        (
            await session.execute(
                select(UserRow.id).where(
                    func.lower(UserRow.github_username) == github_username.lower(),
                    UserRow.deactivated_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


_AttributionFailureReason = Literal[
    "blank_username", "no_matching_user", "username_collision", "not_an_active_member"
]


class GithubAttributionFailedPayload(BaseModel):
    """Audit payload for `ticket.attribution_failed`."""

    github_username: str
    reason: _AttributionFailureReason


async def resolve_github_attribution(
    github_username: str, *, org_id: UUID, ticket_id: UUID, session: AsyncSession
) -> UUID | None:
    """Resolve a GitHub username to the single active org member it belongs to.

    Returns ``None`` — after writing a `ticket.attribution_failed` audit row
    recording why — when the username is blank, matches zero users, matches
    multiple users (collision), or the single match is not an active member
    of ``org_id``. Per-user credential modes depend on attribution, so an
    unresolvable author must be operator-visible, not silent.
    """
    reason: _AttributionFailureReason
    if not github_username:
        reason = "blank_username"
    else:
        user_ids = await find_user_ids_by_github_username(github_username, session=session)
        if len(user_ids) == 0:
            reason = "no_matching_user"
        elif len(user_ids) > 1:
            reason = "username_collision"
        elif user_ids[0] in set(await list_active_member_ids(session, org_id)):
            return user_ids[0]
        else:
            reason = "not_an_active_member"

    actor = Actor.github_user(github_username) if github_username else Actor.system()
    await audit_for_ticket(
        ticket_id,
        "ticket.attribution_failed",
        GithubAttributionFailedPayload(github_username=github_username, reason=reason),
        actor=actor,
        org_id=org_id,
        session=session,
    )
    return None


async def _delete_user_artifacts_for_tests(db: AsyncSession, *, user_id: UUID) -> None:
    """Delete all identity-owned rows for `user_id` (user, emails, OAuth
    identities, sessions). DB-level CASCADE handles child rows when deleting
    the user row via SQL DELETE — callers that need cross-module cleanup
    (e.g. memberships) must handle those separately. Test-only helper."""
    await db.execute(delete(UserRow).where(UserRow.id == user_id))
