"""Service entry-points for `domain/orgs`.

Skeleton at Phase 1 — concrete invite/accept/role flows ship in Phase 6.
Re-exports the public types so callers import from `app.domain.orgs`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.orgs.models import InvitationRow, MembershipRow, OrgRow, SsoConfigRow
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

    @classmethod
    def from_row(cls, row: OrgRow) -> Org:
        return cls(
            id=row.id,
            slug=row.slug,
            display_name=row.display_name,
            archived_at=row.archived_at,
            created_at=row.created_at,
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
]
