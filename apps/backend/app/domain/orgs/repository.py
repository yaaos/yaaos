"""Row access for `domain/orgs` — invitation and SSO-config rows.

Org and membership state (insert/get/update/delete) is delegated to
`core/tenancy`. This module exposes convenience shims so existing callers
(`invitations.py`, `web.py`, `sso_web.py`, tests) can reach tenancy
primitives without a bulk import-site rewrite. New code should import
from `core/tenancy` directly.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Role
from app.core.tenancy import OrgFullView, OrgMembershipInfo
from app.core.tenancy import change_role as _tenancy_change_role
from app.core.tenancy import create_membership as _tenancy_create_membership
from app.core.tenancy import create_org as _tenancy_create_org
from app.core.tenancy import get_membership_info as _tenancy_get_membership_info
from app.core.tenancy import get_org_full as _tenancy_get_org_full
from app.core.tenancy import get_org_full_by_slug as _tenancy_get_org_full_by_slug
from app.core.tenancy import list_memberships_for_org as _tenancy_list_memberships_for_org
from app.core.tenancy import list_memberships_for_user as _tenancy_list_memberships_for_user
from app.core.tenancy import remove_member as _tenancy_remove_member
from app.domain.orgs.models import InvitationRow, SsoConfigRow


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Org / membership shims ───────────────────────────────────────────────────


async def insert_org(session: AsyncSession, *, slug: str, display_name: str = "") -> OrgFullView:
    """Insert org row via core/tenancy. Returns OrgFullView."""
    org_ref = await _tenancy_create_org(session, slug=slug, display_name=display_name or slug)
    full = await _tenancy_get_org_full(session, org_ref.org_id)
    assert full is not None
    return full


async def get_org_by_slug(session: AsyncSession, slug: str) -> OrgFullView | None:
    return await _tenancy_get_org_full_by_slug(session, slug)


async def get_org(session: AsyncSession, org_id: UUID) -> OrgFullView | None:
    return await _tenancy_get_org_full(session, org_id)


async def insert_membership(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
    role: Role,
    handle: str,
) -> None:
    """Insert membership row via core/tenancy."""
    await _tenancy_create_membership(session, user_id=user_id, org_id=org_id, role=role, handle=handle)


async def get_membership(session: AsyncSession, *, user_id: UUID, org_id: UUID) -> OrgMembershipInfo | None:
    """Return the full membership projection, or None."""
    return await _tenancy_get_membership_info(session, user_id=user_id, org_id=org_id)


async def list_memberships_for_user(session: AsyncSession, user_id: UUID) -> list:
    """Return MembershipView list via core/tenancy."""
    return await _tenancy_list_memberships_for_user(session, user_id)


async def list_memberships_for_org(session: AsyncSession, org_id: UUID) -> list[OrgMembershipInfo]:
    """Return OrgMembershipInfo list via core/tenancy."""
    return await _tenancy_list_memberships_for_org(session, org_id)


async def delete_membership(session: AsyncSession, *, user_id: UUID, org_id: UUID) -> None:
    await _tenancy_remove_member(session, user_id=user_id, org_id=org_id)


async def update_role(session: AsyncSession, *, user_id: UUID, org_id: UUID, role: Role) -> OrgMembershipInfo:
    await _tenancy_change_role(session, user_id=user_id, org_id=org_id, role=role)
    return OrgMembershipInfo(user_id=user_id, org_id=org_id, role=role, handle="")


# ── Invitation CRUD ───────────────────────────────────────────────────────────


async def insert_invitation(
    session: AsyncSession,
    *,
    org_id: UUID,
    email: str,
    role: Role,
    token_hash: str,
    expires_at: datetime,
    invited_by_user_id: UUID | None,
) -> InvitationRow:
    row = InvitationRow(
        org_id=org_id,
        email=email,
        role=role.value,
        token_hash=token_hash,
        expires_at=expires_at,
        invited_by_user_id=invited_by_user_id,
    )
    session.add(row)
    await session.flush()
    return row


async def get_invitation_by_token_hash(session: AsyncSession, token_hash: str) -> InvitationRow | None:
    return (
        await session.execute(select(InvitationRow).where(InvitationRow.token_hash == token_hash))
    ).scalar_one_or_none()


async def get_sso_config(session: AsyncSession, org_id: UUID) -> SsoConfigRow | None:
    return (
        await session.execute(select(SsoConfigRow).where(SsoConfigRow.org_id == org_id))
    ).scalar_one_or_none()
