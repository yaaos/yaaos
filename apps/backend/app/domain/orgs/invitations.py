"""Invitation + membership lifecycle for `domain/orgs`.

Owns `invite`, `accept_invitation`, `remove_member`, `change_role`. Each
emits an audit-log entry via `core/audit_log.audit()`. Role-change rotates
the affected user's sessions; member removal revokes every session for the
user.

Tokens are signed via `itsdangerous.URLSafeTimedSerializer` with a 7-day TTL
and the in-DB row marks `accepted_at` on use — replaying a signed token after
acceptance returns 410.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.config import get_settings
from app.domain.identity import sessions as session_lifecycle
from app.domain.orgs import email as org_email
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.service import Invitation, Membership
from app.domain.orgs.types import InvitationError, Role

log = structlog.get_logger("orgs.invitations")


class InvitationExpiredError(InvitationError):
    """Token is past its TTL or the row's `expires_at`."""


class InvitationUsedError(InvitationError):
    """Token has already been accepted (`accepted_at` is set)."""


class InvitationInvalidError(InvitationError):
    """Token signature didn't verify, or no matching row."""


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().yaaos_invitation_token_secret, salt="yaaos-invitation")


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class _InvitePayload(BaseModel):
    email: str
    role: Role
    expires_at: datetime
    invited_by_user_id: UUID | None


class _MembershipChangePayload(BaseModel):
    user_id: UUID
    from_role: Role | None = None
    to_role: Role | None = None


async def invite(
    db: AsyncSession,
    *,
    org_id: UUID,
    email: str,
    role: Role,
    invited_by_user_id: UUID | None,
    actor: Actor,
) -> tuple[Invitation, str]:
    """Create an invitation row, send the email, write an audit entry.

    Returns the persisted `Invitation` plus the **raw** signed token (so
    callers in tests can hit `accept_invitation` directly). In normal use the
    raw token lives only in the email — the DB stores its sha256 hex.
    """
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.yaaos_invitation_lifetime_seconds)
    raw_token = _serializer().dumps({"org_id": str(org_id), "email": email.lower()})

    row = await orgs_repo.insert_invitation(
        db,
        org_id=org_id,
        email=email.lower(),
        role=role,
        token_hash=_hash(raw_token),
        expires_at=expires_at,
        invited_by_user_id=invited_by_user_id,
    )

    accept_url = f"{settings.yaaos_app_base_url}/invitations/accept?token={raw_token}"
    await org_email.send_plain(
        to=email,
        subject=f"You're invited to {org_id}",
        body=f"Accept your invitation: {accept_url}\n\nLink expires in 7 days.",
    )

    await audit(
        "invitation",
        row.id,
        "invited",
        _InvitePayload(
            email=email.lower(),
            role=role,
            expires_at=expires_at,
            invited_by_user_id=invited_by_user_id,
        ),
        actor,
        org_id=org_id,
        session=db,
    )
    return Invitation.from_row(row), raw_token


async def accept_invitation(
    db: AsyncSession,
    *,
    raw_token: str,
    user_id: UUID,
    actor: Actor,
) -> Membership:
    """Validate the signed token, create the membership, mark accepted.

    Raises:
      InvitationExpiredError — TTL exceeded or row `expires_at` past.
      InvitationUsedError — `accepted_at` is already set.
      InvitationInvalidError — bad signature, no row, or email/org mismatch.
    """
    settings = get_settings()
    try:
        payload = _serializer().loads(raw_token, max_age=settings.yaaos_invitation_lifetime_seconds)
    except SignatureExpired as exc:
        raise InvitationExpiredError("token signature expired") from exc
    except BadSignature as exc:
        raise InvitationInvalidError("token signature invalid") from exc

    row = await orgs_repo.get_invitation_by_token_hash(db, _hash(raw_token))
    if row is None:
        raise InvitationInvalidError("no invitation matches the token")
    if row.accepted_at is not None:
        raise InvitationUsedError("invitation already accepted")
    if row.expires_at < datetime.now(UTC):
        raise InvitationExpiredError("invitation expired")
    if str(row.org_id) != payload.get("org_id") or row.email.lower() != payload.get("email", "").lower():
        raise InvitationInvalidError("token does not match invitation row")

    # Idempotent: if a membership already exists, bail with the existing row.
    existing = await orgs_repo.get_membership(db, user_id=user_id, org_id=row.org_id)
    if existing is not None:
        row.accepted_at = datetime.now(UTC)
        await db.flush()
        return Membership.from_row(existing)

    handle = row.email.split("@", 1)[0][:64].lower()
    membership_row = await orgs_repo.insert_membership(
        db, user_id=user_id, org_id=row.org_id, role=Role(row.role), handle=handle
    )
    row.accepted_at = datetime.now(UTC)
    await db.flush()

    await audit(
        "membership",
        membership_row.user_id,
        "joined",
        _MembershipChangePayload(user_id=user_id, to_role=Role(row.role)),
        actor,
        org_id=row.org_id,
        session=db,
    )
    return Membership.from_row(membership_row)


async def remove_member(
    db: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    actor: Actor,
) -> None:
    """Delete the membership, revoke every session belonging to the user,
    write an audit row. No-op if the membership is already gone."""
    existing = await orgs_repo.get_membership(db, user_id=user_id, org_id=org_id)
    if existing is None:
        return
    from_role = Role(existing.role)
    await orgs_repo.delete_membership(db, user_id=user_id, org_id=org_id)
    await session_lifecycle.revoke_all_for_user(db, user_id)

    await audit(
        "membership",
        user_id,
        "removed",
        _MembershipChangePayload(user_id=user_id, from_role=from_role),
        actor,
        org_id=org_id,
        session=db,
    )


async def change_role(
    db: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    new_role: Role,
    actor: Actor,
) -> Membership:
    """Update the membership row, rotate the affected user's sessions, audit.

    Rotation = revoke + create fresh; the affected user is signed out
    everywhere they were and must re-authenticate. Phase 12 will swap the
    blunt rotation for a session-row update that flips `sso_satisfied_*` and
    the role-derived claims without forcing re-auth — for the POC, "you got
    promoted, sign in again" is fine.
    """
    existing = await orgs_repo.get_membership(db, user_id=user_id, org_id=org_id)
    if existing is None:
        raise LookupError("membership not found")
    from_role = Role(existing.role)
    row = await orgs_repo.update_role(db, user_id=user_id, org_id=org_id, role=new_role)
    await session_lifecycle.revoke_all_for_user(db, user_id)

    await audit(
        "membership",
        user_id,
        "role_changed",
        _MembershipChangePayload(user_id=user_id, from_role=from_role, to_role=new_role),
        actor,
        org_id=org_id,
        session=db,
    )
    return Membership.from_row(row)


__all__ = [
    "InvitationExpiredError",
    "InvitationInvalidError",
    "InvitationUsedError",
    "accept_invitation",
    "change_role",
    "invite",
    "remove_member",
]
