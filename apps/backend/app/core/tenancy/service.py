"""Service entry-points for `core/tenancy`.

Owns the org/membership access graph. Returns value objects only — never
SQLAlchemy Row types. All write functions take a required `session: AsyncSession`
and never commit (shape (a) per `apps/backend/docs/patterns.md`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
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
    get_org_row_by_iam_arn,
    get_org_row_by_slug,
    insert_membership,
    insert_org,
    list_active_member_id_rows,
    list_memberships_for_org_rows,
    list_memberships_for_user_rows,
    set_sso_authz,
    update_member_handle_row,
    update_role,
)
from app.core.tenancy.repository import (
    delete_org as _delete_org_row,
)

# ── Sentinels ────────────────────────────────────────────────────────────────


class _Unset:
    """Sentinel distinguishing "leave this column unchanged" from "set to None".

    Used by `update_org_fields` so an explicit `None` clears a nullable column
    while an omitted argument is left untouched.
    """


_UNSET: Any = _Unset()


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
    # Per-org idle session timeout (minutes). None = use the global default.
    session_timeout_override: int | None = None


class MembershipView(BaseModel):
    """User's membership list item — org identity + role + handle."""

    org_id: UUID
    slug: str
    org_name: str
    role: Role
    handle: str


class OrgMembershipInfo(BaseModel):
    """Per-org membership projection — identity + role + handle.

    Returned by `list_memberships_for_org`. Does not carry org name/slug;
    callers already have org context.
    """

    user_id: UUID
    org_id: UUID
    role: Role
    handle: str


class OrgFullView(BaseModel):
    """Full read-side view of an org row including feature-level columns.

    Returned by `get_org_full`, `update_org_fields`, and VCS state helpers so
    `domain/orgs` callers never need to import `OrgRow` directly.
    """

    org_id: UUID
    slug: str
    display_name: str
    session_timeout_override: int | None = None
    workspace_provider: str | None = None
    registered_iam_arn: str | None = None
    aws_region: str | None = None
    vcs_plugin_id: str | None = None
    vcs_settings: dict | None = None

    @classmethod
    def from_row(cls, row: OrgRow) -> OrgFullView:
        return cls(
            org_id=row.id,
            slug=row.slug,
            display_name=row.display_name,
            session_timeout_override=row.session_timeout_override,
            workspace_provider=row.workspace_provider,
            registered_iam_arn=row.registered_iam_arn,
            aws_region=row.aws_region,
            vcs_plugin_id=row.vcs_plugin_id,
            vcs_settings=dict(row.vcs_settings) if row.vcs_settings else None,
        )


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
        session_timeout_override=org_row.session_timeout_override,
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


async def get_membership_info(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
) -> OrgMembershipInfo | None:
    """Return the full membership projection for (user_id, org_id), or None."""
    row = await get_membership(session, user_id=user_id, org_id=org_id)
    if row is None:
        return None
    return OrgMembershipInfo(user_id=row.user_id, org_id=row.org_id, role=Role(row.role), handle=row.handle)


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


async def list_memberships_for_org(session: AsyncSession, org_id: UUID) -> list[OrgMembershipInfo]:
    """Return all memberships for *org_id* as `OrgMembershipInfo` VOs."""
    rows = await list_memberships_for_org_rows(session, org_id)
    return [
        OrgMembershipInfo(user_id=r.user_id, org_id=r.org_id, role=Role(r.role), handle=r.handle)
        for r in rows
    ]


async def update_member_handle(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
    handle: str,
) -> OrgMembershipInfo:
    """Update a member's @handle. Raises `LookupError` if no membership exists."""
    row = await update_member_handle_row(session, user_id=user_id, org_id=org_id, handle=handle)
    return OrgMembershipInfo(user_id=row.user_id, org_id=row.org_id, role=Role(row.role), handle=row.handle)


async def get_org_full(session: AsyncSession, org_id: UUID) -> OrgFullView | None:
    """Return the full org view including feature columns, or None."""
    row = await get_org_row(session, org_id)
    return OrgFullView.from_row(row) if row is not None else None


async def get_org_full_by_slug(session: AsyncSession, slug: str) -> OrgFullView | None:
    """Return the full org view by slug, or None."""
    row = await get_org_row_by_slug(session, slug)
    return OrgFullView.from_row(row) if row is not None else None


async def update_org_fields(
    session: AsyncSession,
    org_id: UUID,
    *,
    session_timeout_override: int | None | _Unset = _UNSET,
    workspace_provider: str | None | _Unset = _UNSET,
    registered_iam_arn: str | None | _Unset = _UNSET,
    aws_region: str | None | _Unset = _UNSET,
    archived_at: datetime | None | _Unset = _UNSET,
) -> OrgFullView:
    """Update the mutable columns on an org row, by explicit keyword.

    Each argument defaults to a sentinel meaning "leave unchanged"; pass an
    explicit value (including `None`) to set the column. Raises `LookupError`
    if the org is not found. Does not commit — shape (a).
    """
    row = await get_org_row(session, org_id)
    if row is None:
        raise LookupError(f"org {org_id} not found")
    if not isinstance(session_timeout_override, _Unset):
        row.session_timeout_override = session_timeout_override
    if not isinstance(workspace_provider, _Unset):
        row.workspace_provider = workspace_provider
    if not isinstance(registered_iam_arn, _Unset):
        row.registered_iam_arn = registered_iam_arn
    if not isinstance(aws_region, _Unset):
        row.aws_region = aws_region
    if not isinstance(archived_at, _Unset):
        row.archived_at = archived_at
    await session.flush()
    await session.refresh(row)
    return OrgFullView.from_row(row)


async def get_vcs_state(session: AsyncSession, org_id: UUID) -> tuple[str | None, dict]:
    """Return (plugin_id, settings) for the org's current VCS choice."""
    row = await get_org_row(session, org_id)
    if row is None:
        raise LookupError(f"org {org_id} not found")
    return row.vcs_plugin_id, dict(row.vcs_settings or {})


async def set_vcs_state(
    session: AsyncSession,
    org_id: UUID,
    *,
    plugin_id: str,
    settings: dict,
) -> None:
    """Persist the VCS plugin choice + settings on the org row."""
    from sqlalchemy import update as sql_update  # noqa: PLC0415

    await session.execute(
        sql_update(OrgRow).where(OrgRow.id == org_id).values(vcs_plugin_id=plugin_id, vcs_settings=settings)
    )
    await session.flush()


async def clear_vcs_state(session: AsyncSession, org_id: UUID) -> str | None:
    """Clear the org's VCS choice. Returns the prior plugin_id, or None if none was set."""
    row = await get_org_row(session, org_id)
    if row is None:
        raise LookupError(f"org {org_id} not found")
    prior = row.vcs_plugin_id
    if prior is None:
        return None
    row.vcs_plugin_id = None
    row.vcs_settings = None
    await session.flush()
    return prior


async def _delete_org_for_tests(session: AsyncSession, org_id: UUID) -> None:
    """Hard-delete an org row. Cascades to memberships, invitations, etc.

    Test-only teardown helper — there is no production org-deletion flow yet.
    Underscored and deliberately kept out of `core/tenancy`'s public `__all__`
    so it can't be mistaken for a feature API (a real delete would take an
    `Actor` and write an audit row). Tests import it from this submodule.
    """
    await _delete_org_row(session, org_id)


async def get_org_full_by_iam_arn(session: AsyncSession, canonical_arn: str) -> OrgFullView | None:
    """Return the full org view for the org registered with *canonical_arn*, or None.

    Callers must canonicalize the ARN (lowercase) before calling.
    """
    row = await get_org_row_by_iam_arn(session, canonical_arn)
    if row is None:
        return None
    return OrgFullView.from_row(row)
