"""Per-org installed coding-agent plugins.

Many coding-agent plugins per org. Each install lives in `org_coding_agents`
(`(org_id, plugin_id) PK`, `settings jsonb`, ...). Every mutation emits an
audit-log entry.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.domain.orgs.models import OrgCodingAgentRow

log = structlog.get_logger("orgs.coding_agents")


class CodingAgentInstall(BaseModel):
    org_id: UUID
    plugin_id: str
    settings: dict
    created_at: datetime
    updated_at: datetime
    created_by: UUID | None


class CodingAgentAuditPayload(BaseModel):
    plugin_id: str


class CodingAgentAlreadyInstalledError(ValueError):
    """The org already has this coding-agent plugin installed."""


class CodingAgentNotInstalledError(LookupError):
    """The org has no install for this coding-agent plugin."""


def _from_row(row: OrgCodingAgentRow) -> CodingAgentInstall:
    return CodingAgentInstall(
        org_id=row.org_id,
        plugin_id=row.plugin_id,
        settings=dict(row.settings or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
    )


async def list_coding_agents(session: AsyncSession, org_id: UUID) -> list[CodingAgentInstall]:
    rows = (
        (
            await session.execute(
                select(OrgCodingAgentRow)
                .where(OrgCodingAgentRow.org_id == org_id)
                .order_by(OrgCodingAgentRow.plugin_id)
            )
        )
        .scalars()
        .all()
    )
    return [_from_row(r) for r in rows]


async def install_coding_agent(
    session: AsyncSession,
    *,
    org_id: UUID,
    plugin_id: str,
    settings: dict,
    actor: Actor,
    created_by: UUID | None = None,
) -> CodingAgentInstall:
    """Insert a new install. Raises `CodingAgentAlreadyInstalledError` if a
    row already exists. Emits `coding_agent.installed`."""
    existing = (
        await session.execute(
            select(OrgCodingAgentRow).where(
                OrgCodingAgentRow.org_id == org_id,
                OrgCodingAgentRow.plugin_id == plugin_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise CodingAgentAlreadyInstalledError(plugin_id)
    row = OrgCodingAgentRow(
        org_id=org_id,
        plugin_id=plugin_id,
        settings=settings,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await audit(
        "org",
        org_id,
        "coding_agent.installed",
        CodingAgentAuditPayload(plugin_id=plugin_id),
        actor,
        org_id=org_id,
        session=session,
    )
    return _from_row(row)


async def update_coding_agent_settings(
    session: AsyncSession,
    *,
    org_id: UUID,
    plugin_id: str,
    settings: dict,
    actor: Actor,
) -> CodingAgentInstall:
    """Replace the install's settings. Emits `coding_agent.settings_updated`."""
    row = (
        await session.execute(
            select(OrgCodingAgentRow).where(
                OrgCodingAgentRow.org_id == org_id,
                OrgCodingAgentRow.plugin_id == plugin_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise CodingAgentNotInstalledError(plugin_id)
    row.settings = settings
    await session.flush()
    await session.refresh(row)
    await audit(
        "org",
        org_id,
        "coding_agent.settings_updated",
        CodingAgentAuditPayload(plugin_id=plugin_id),
        actor,
        org_id=org_id,
        session=session,
    )
    return _from_row(row)


async def uninstall_coding_agent(
    session: AsyncSession,
    *,
    org_id: UUID,
    plugin_id: str,
    actor: Actor,
) -> bool:
    """Remove the install. Returns True if a row was removed. Emits
    `coding_agent.uninstalled` only when something was actually removed."""
    result = await session.execute(
        delete(OrgCodingAgentRow).where(
            OrgCodingAgentRow.org_id == org_id,
            OrgCodingAgentRow.plugin_id == plugin_id,
        )
    )
    removed = bool(result.rowcount)
    if removed:
        await audit(
            "org",
            org_id,
            "coding_agent.uninstalled",
            CodingAgentAuditPayload(plugin_id=plugin_id),
            actor,
            org_id=org_id,
            session=session,
        )
    return removed
