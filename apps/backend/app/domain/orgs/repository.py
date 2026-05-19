"""Raw row access for `domain/orgs`."""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.orgs.models import InvitationRow, MembershipRow, OrgRow, SsoConfigRow
from app.domain.orgs.types import Role


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def insert_org(session: AsyncSession, *, slug: str, display_name: str = "") -> OrgRow:
    row = OrgRow(id=uuid4(), slug=slug, display_name=display_name or slug)
    session.add(row)
    await session.flush()
    return row


async def get_org_by_slug(session: AsyncSession, slug: str) -> OrgRow | None:
    return (
        await session.execute(select(OrgRow).where(OrgRow.slug == slug, OrgRow.archived_at.is_(None)))
    ).scalar_one_or_none()


async def get_org(session: AsyncSession, org_id: UUID) -> OrgRow | None:
    return (await session.execute(select(OrgRow).where(OrgRow.id == org_id))).scalar_one_or_none()


async def insert_membership(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
    role: Role,
    handle: str,
) -> MembershipRow:
    row = MembershipRow(user_id=user_id, org_id=org_id, role=role.value, handle=handle)
    session.add(row)
    await session.flush()
    return row


async def get_membership(session: AsyncSession, *, user_id: UUID, org_id: UUID) -> MembershipRow | None:
    return (
        await session.execute(
            select(MembershipRow).where(MembershipRow.user_id == user_id, MembershipRow.org_id == org_id)
        )
    ).scalar_one_or_none()


async def list_memberships_for_user(session: AsyncSession, user_id: UUID) -> list[MembershipRow]:
    return list(
        (await session.execute(select(MembershipRow).where(MembershipRow.user_id == user_id))).scalars().all()
    )


async def list_memberships_for_org(session: AsyncSession, org_id: UUID) -> list[MembershipRow]:
    return list(
        (await session.execute(select(MembershipRow).where(MembershipRow.org_id == org_id))).scalars().all()
    )


async def delete_membership(session: AsyncSession, *, user_id: UUID, org_id: UUID) -> None:
    row = await get_membership(session, user_id=user_id, org_id=org_id)
    if row is not None:
        await session.delete(row)
        await session.flush()


async def update_role(session: AsyncSession, *, user_id: UUID, org_id: UUID, role: Role) -> MembershipRow:
    row = await get_membership(session, user_id=user_id, org_id=org_id)
    if row is None:
        raise LookupError("membership not found")
    row.role = role.value
    await session.flush()
    return row


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
        id=uuid4(),
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
