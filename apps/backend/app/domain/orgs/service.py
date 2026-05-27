"""Service entry-points for `domain/orgs`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete as sql_delete

from app.core.database import session as db_session
from app.domain.orgs.models import InvitationRow, MembershipRow, OrgRow, SsoConfigRow
from app.domain.orgs.repository import get_org as _repo_get_org
from app.domain.orgs.types import (
    InsufficientRoleError,
    InvitationError,
    MembershipNotFoundError,
    OrgNotFoundError,
    Role,
)


class Org(BaseModel):
    id: UUID
    slug: str
    display_name: str
    archived_at: datetime | None
    created_at: datetime
    workspace_provider: str | None

    @classmethod
    def from_row(cls, row: OrgRow) -> Org:
        return cls(
            id=row.id,
            slug=row.slug,
            display_name=row.display_name,
            archived_at=row.archived_at,
            created_at=row.created_at,
            workspace_provider=row.workspace_provider,
        )


class Membership(BaseModel):
    user_id: UUID
    org_id: UUID
    role: Role
    handle: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: MembershipRow) -> Membership:
        return cls(
            user_id=row.user_id,
            org_id=row.org_id,
            role=Role(row.role),
            handle=row.handle,
            created_at=row.created_at,
        )


class Invitation(BaseModel):
    id: UUID
    org_id: UUID
    email: str
    role: Role
    expires_at: datetime
    accepted_at: datetime | None
    invited_by_user_id: UUID | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: InvitationRow) -> Invitation:
        return cls(
            id=row.id,
            org_id=row.org_id,
            email=row.email,
            role=Role(row.role),
            expires_at=row.expires_at,
            accepted_at=row.accepted_at,
            invited_by_user_id=row.invited_by_user_id,
            created_at=row.created_at,
        )


class SsoConfig(BaseModel):
    org_id: UUID
    enabled: bool
    jit_enabled: bool
    exempt_owner_user_id: UUID | None
    updated_at: datetime

    @classmethod
    def from_row(cls, row: SsoConfigRow) -> SsoConfig:
        return cls(
            org_id=row.org_id,
            enabled=row.enabled,
            jit_enabled=row.jit_enabled,
            exempt_owner_user_id=row.exempt_owner_user_id,
            updated_at=row.updated_at,
        )


async def get_org(org_id: UUID) -> Org | None:
    """Return the `Org` value object for *org_id*, or ``None`` if not found."""
    async with db_session() as s:
        row = await _repo_get_org(s, org_id)
    return Org.from_row(row) if row is not None else None


async def delete_expired_invitations() -> int:
    """Delete all unaccepted, past-expiry invitations. Returns the count deleted."""
    async with db_session() as s:
        result = await s.execute(
            sql_delete(InvitationRow)
            .where(
                InvitationRow.expires_at < datetime.now(UTC),
                InvitationRow.accepted_at.is_(None),
            )
            .returning(InvitationRow.id)
        )
        n = len(result.all())
        await s.commit()
        return n


__all__ = [
    "InsufficientRoleError",
    "Invitation",
    "InvitationError",
    "Membership",
    "MembershipNotFoundError",
    "Org",
    "OrgNotFoundError",
    "Role",
    "SsoConfig",
    "delete_expired_invitations",
    "get_org",
]
