"""Service entry-points for `domain/orgs`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.auth import Role
from app.core.database import session as db_session
from app.core.tenancy.models import MembershipRow, OrgRow
from app.domain.orgs.models import InvitationRow, SsoConfigRow
from app.domain.orgs.repository import get_org as _repo_get_org
from app.domain.orgs.types import (
    InsufficientRoleError,
    InvitationError,
    MembershipNotFoundError,
    OrgNotFoundError,
)


class Org(BaseModel):
    id: UUID
    slug: str
    display_name: str
    archived_at: datetime | None
    created_at: datetime
    workspace_provider: str | None
    session_timeout_override: int | None = None

    @classmethod
    def from_row(cls, row: OrgRow) -> Org:
        return cls(
            id=row.id,
            slug=row.slug,
            display_name=row.display_name,
            archived_at=row.archived_at,
            created_at=row.created_at,
            workspace_provider=row.workspace_provider,
            session_timeout_override=row.session_timeout_override,
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
    idp_metadata_xml: str
    email_domains: list[str]
    updated_at: datetime

    @classmethod
    def from_row(cls, row: SsoConfigRow) -> SsoConfig:
        return cls(
            org_id=row.org_id,
            enabled=row.enabled,
            jit_enabled=row.jit_enabled,
            exempt_owner_user_id=row.exempt_owner_user_id,
            idp_metadata_xml=row.idp_metadata_xml,
            email_domains=list(row.email_domains or []),
            updated_at=row.updated_at,
        )


class _OrgCreatedPayload(BaseModel):
    slug: str
    display_name: str


class _MembershipCreatedPayload(BaseModel):
    role: str


async def create_org(
    session: AsyncSession,
    *,
    slug: str,
    display_name: str,
    actor: Actor = Actor.system(),
) -> Org:
    """Insert a new org row. Emits ``org.created`` audit entry.

    Shape (a) — takes ``session`` first positional; never commits. Caller
    composes with sibling writes inside one ``async with db_session()`` block.
    See `apps/backend/docs/patterns.md` § Service-fn session-handling convention.
    """
    row = OrgRow(id=uuid4(), slug=slug, display_name=display_name)
    session.add(row)
    await session.flush()
    await audit(
        "org",
        row.id,
        "org.created",
        _OrgCreatedPayload(slug=slug, display_name=display_name),
        actor,
        org_id=row.id,
        session=session,
    )
    return Org.from_row(row)


async def create_membership(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID,
    role: Role,
    handle: str,
    actor: Actor = Actor.system(),
) -> Membership:
    """Insert a membership row directly, bypassing the invitation flow.

    Intended for bootstrap-style setup where the owner is already known
    (e.g. the admin onboarding path or e2e seeding). Emits
    ``membership.created`` audit entry.

    Shape (a) — takes ``session`` first positional; never commits.
    See `apps/backend/docs/patterns.md` § Service-fn session-handling convention.
    """
    row = MembershipRow(user_id=user_id, org_id=org_id, role=role.value, handle=handle)
    session.add(row)
    await session.flush()
    await audit(
        "org",
        org_id,
        "membership.created",
        _MembershipCreatedPayload(role=role.value),
        actor,
        org_id=org_id,
        session=session,
    )
    return Membership.from_row(row)


async def get_org(org_id: UUID) -> Org | None:
    """Return the `Org` value object for *org_id*, or ``None`` if not found."""
    async with db_session() as s:
        return await _repo_get_org(s, org_id)


async def get_org_by_slug(slug: str) -> Org | None:
    """Return the `Org` value object for *slug*, or ``None`` if not found.

    Callers outside `domain/orgs` should use this rather than the repository
    directly — the service layer is the public boundary.
    """
    from app.domain.orgs.repository import get_org_by_slug as _repo_get_by_slug  # noqa: PLC0415

    async with db_session() as s:
        return await _repo_get_by_slug(s, slug)


async def _lookup_org_by_arn(canonical_arn: str) -> object:
    """Backing function for the `core/agent_gateway` ARN-lookup registry.

    Looks up the org whose `registered_iam_arn` equals *canonical_arn*
    (case-sensitive; callers must canonicalize to lowercase first) and
    returns a `core/agent_gateway.OrgArnRef` VO. Returns ``None`` when no
    match.

    Registered into `core/agent_gateway.register_org_arn_lookup` at module
    import so the identity-exchange handler never needs a `core → domain`
    import. Return type is `object` to avoid a forward-reference to the
    core module at annotation evaluation time.
    """
    from app.core.agent_gateway import OrgArnRef  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(
                select(OrgRow).where(
                    OrgRow.registered_iam_arn == canonical_arn,
                    OrgRow.archived_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return OrgArnRef(id=row.id, aws_region=row.aws_region)


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


async def find_saml_org_slug_for_domain(domain: str) -> str | None:
    """Return the org slug for the SAML SSO config that covers *domain*, or
    ``None`` when no enabled config matches.

    Scans enabled `sso_configs` rows whose `email_domains` JSONB array contains
    *domain* (case-sensitive; callers must normalize before calling). Returns the
    first match — at most one enabled config per domain is expected.
    """
    from sqlalchemy import select  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(
                select(SsoConfigRow, OrgRow)
                .join(OrgRow, OrgRow.id == SsoConfigRow.org_id)
                .where(SsoConfigRow.enabled.is_(True))
                .where(SsoConfigRow.email_domains.op("?")(domain))
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    _, org = row
    return org.slug


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
    "create_membership",
    "create_org",
    "delete_expired_invitations",
    "find_saml_org_slug_for_domain",
    "get_org",
    "get_org_by_slug",
]
