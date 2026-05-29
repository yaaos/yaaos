"""Service entry-points for `core/tenancy`.

Owns the org/membership access graph. Returns value objects only — never
SQLAlchemy Row types. All write functions take a required `session: AsyncSession`
and never commit (shape (a) per `apps/backend/docs/patterns.md`).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Role
from app.core.tenancy.models import OrgRow
from app.core.tenancy.repository import (
    delete_membership,
    get_membership,
    get_org_row,
    get_org_row_by_slug,
    insert_membership,
    insert_org,
    list_active_member_id_rows,
    list_memberships_for_user_rows,
    set_sso_authz,
    update_role,
)

# ── Value objects ────────────────────────────────────────────────────────────


class OrgRef(BaseModel):
    """Caller-agnostic org identity — id + immutable address (slug + name)."""

    org_id: UUID
    slug: str
    name: str

    @classmethod
    def from_row(cls, row: OrgRow) -> OrgRef:
        return cls(org_id=row.id, slug=row.slug, name=row.display_name)


class AuthOrg(BaseModel):
    """Per-caller authz projection consumed by `require()` and SSO middleware."""

    org_id: UUID
    slug: str
    role: Role
    sso_enabled: bool
    sso_exempt_owner_user_id: UUID | None


class MembershipView(BaseModel):
    """User's membership list item — org identity + role + handle."""

    org_id: UUID
    slug: str
    org_name: str
    role: Role
    handle: str


# ── Exceptions ───────────────────────────────────────────────────────────────


class OrgNotFoundError(LookupError):
    """Slug → org lookup failed, or caller has no membership."""


class MembershipNotFoundError(LookupError):
    """No membership for (user_id, org_id)."""


# ── Primitives ───────────────────────────────────────────────────────────────


async def resolve_auth_org(
    session: AsyncSession,
    *,
    user_id: UUID,
    slug: str,
) -> AuthOrg | None:
    """Return the per-caller authz projection for (user_id, org slug), or None.

    Returns None when the org does not exist or the user has no membership.
    """
    org_row = await get_org_row_by_slug(session, slug)
    if org_row is None:
        return None
    membership = await get_membership(session, user_id=user_id, org_id=org_row.id)
    if membership is None:
        return None
    return AuthOrg(
        org_id=org_row.id,
        slug=org_row.slug,
        role=Role(membership.role),
        sso_enabled=org_row.sso_enabled,
        sso_exempt_owner_user_id=org_row.sso_exempt_owner_user_id,
    )


async def get_org_by_slug(session: AsyncSession, slug: str) -> OrgRef | None:
    """Return the OrgRef for *slug*, or None if not found / archived."""
    row = await get_org_row_by_slug(session, slug)
    return OrgRef.from_row(row) if row is not None else None


async def get_org(session: AsyncSession, org_id: UUID) -> OrgRef | None:
    """Return the OrgRef for *org_id*, or None if not found."""
    row = await get_org_row(session, org_id)
    return OrgRef.from_row(row) if row is not None else None


async def get_member_role(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
) -> Role | None:
    """Return the user's role in the org, or None if they have no membership."""
    membership = await get_membership(session, user_id=user_id, org_id=org_id)
    return Role(membership.role) if membership is not None else None


async def list_memberships_for_user(
    session: AsyncSession,
    user_id: UUID,
) -> list[MembershipView]:
    """Return all MembershipView records for a user across all orgs.

    Fetches the org row for each membership to populate slug + name.
    """
    membership_rows = await list_memberships_for_user_rows(session, user_id)
    if not membership_rows:
        return []

    org_ids = [m.org_id for m in membership_rows]
    org_rows = list((await session.execute(select(OrgRow).where(OrgRow.id.in_(org_ids)))).scalars().all())
    org_by_id = {o.id: o for o in org_rows}

    views: list[MembershipView] = []
    for m in membership_rows:
        org = org_by_id.get(m.org_id)
        if org is None:
            continue
        views.append(
            MembershipView(
                org_id=org.id,
                slug=org.slug,
                org_name=org.display_name,
                role=Role(m.role),
                handle=m.handle,
            )
        )
    return views


async def list_active_member_ids(session: AsyncSession, org_id: UUID) -> list[UUID]:
    """Return all user_ids that have an active membership in org_id."""
    return await list_active_member_id_rows(session, org_id)


async def create_org(
    session: AsyncSession,
    *,
    slug: str,
    display_name: str,
) -> OrgRef:
    """Insert a new org row. Returns OrgRef.

    Shape (a) — takes `session` first positional; never commits.
    """
    row = await insert_org(session, slug=slug, display_name=display_name)
    return OrgRef.from_row(row)


async def create_membership(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
    role: Role,
    handle: str,
) -> None:
    """Insert a membership row.

    Shape (a) — takes `session` first positional; never commits.
    """
    await insert_membership(session, user_id=user_id, org_id=org_id, role=role, handle=handle)


async def change_role(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
    role: Role,
) -> None:
    """Update a membership's role.

    Raises `LookupError` if no membership exists.
    """
    await update_role(session, user_id=user_id, org_id=org_id, role=role)


async def remove_member(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
) -> None:
    """Delete a membership row. No-op if already gone."""
    await delete_membership(session, user_id=user_id, org_id=org_id)


async def set_sso_authz_for_org(
    session: AsyncSession,
    *,
    org_id: UUID,
    enabled: bool,
    exempt_owner: UUID | None,
) -> None:
    """Update the denormalized SSO authz columns on the org row.

    Called by `domain/orgs/sso.upsert_config` after committing the
    sso_configs row, keeping the fast-access columns on `orgs` in sync.
    Raises `LookupError` if the org is not found.
    """
    await set_sso_authz(session, org_id=org_id, enabled=enabled, exempt_owner=exempt_owner)
