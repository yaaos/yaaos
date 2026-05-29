"""Raw row access for `core/tenancy` — orgs and memberships tables."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Role
from app.core.tenancy.models import MembershipRow, OrgRow


async def insert_org(session: AsyncSession, *, slug: str, display_name: str = "") -> OrgRow:
    row = OrgRow(slug=slug, display_name=display_name or slug)
    session.add(row)
    await session.flush()
    return row


async def get_org_row_by_slug(session: AsyncSession, slug: str) -> OrgRow | None:
    return (
        await session.execute(select(OrgRow).where(OrgRow.slug == slug, OrgRow.archived_at.is_(None)))
    ).scalar_one_or_none()


async def get_org_row(session: AsyncSession, org_id: UUID) -> OrgRow | None:
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


async def list_memberships_for_user_rows(session: AsyncSession, user_id: UUID) -> list[MembershipRow]:
    return list(
        (await session.execute(select(MembershipRow).where(MembershipRow.user_id == user_id))).scalars().all()
    )


async def list_memberships_for_org_rows(session: AsyncSession, org_id: UUID) -> list[MembershipRow]:
    return list(
        (await session.execute(select(MembershipRow).where(MembershipRow.org_id == org_id))).scalars().all()
    )


async def list_active_member_id_rows(session: AsyncSession, org_id: UUID) -> list[UUID]:
    """Return all user_ids that have a membership in org_id."""
    rows = await list_memberships_for_org_rows(session, org_id)
    return [r.user_id for r in rows]


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


async def get_org_row_by_iam_arn(session: AsyncSession, canonical_arn: str) -> OrgRow | None:
    """Return the OrgRow whose `registered_iam_arn` matches *canonical_arn*, or None."""
    return (
        await session.execute(
            select(OrgRow).where(
                OrgRow.registered_iam_arn == canonical_arn,
                OrgRow.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def set_sso_authz(
    session: AsyncSession,
    *,
    org_id: UUID,
    enabled: bool,
    exempt_owner: UUID | None,
) -> None:
    """Update the denormalized SSO authz columns on the org row."""
    row = await get_org_row(session, org_id)
    if row is None:
        raise LookupError(f"org {org_id} not found")
    row.sso_enabled = enabled
    row.sso_exempt_owner_user_id = exempt_owner
    await session.flush()


async def update_member_handle_row(
    session: AsyncSession, *, user_id: UUID, org_id: UUID, handle: str
) -> MembershipRow:
    """Set a member's handle. Raises `LookupError` if no membership exists."""
    row = await get_membership(session, user_id=user_id, org_id=org_id)
    if row is None:
        raise LookupError("membership not found")
    row.handle = handle
    await session.flush()
    return row


async def delete_org(session: AsyncSession, org_id: UUID) -> None:
    """Hard-delete an org row (cascades to memberships, invitations, etc.)."""
    from sqlalchemy import delete as sql_delete  # noqa: PLC0415

    await session.execute(sql_delete(OrgRow).where(OrgRow.id == org_id))
    await session.flush()
