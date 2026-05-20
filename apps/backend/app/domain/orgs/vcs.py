"""Per-org VCS plugin install state.

One VCS per org. State lives on the `orgs` table (`vcs_plugin_id`,
`vcs_settings`). Switching is explicit: clear, then set. Every mutation emits
an audit-log entry.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.domain.orgs.models import OrgRow

log = structlog.get_logger("orgs.vcs")


class VcsState(BaseModel):
    """Read-side view of an org's VCS choice. `plugin_id` is None when no
    VCS is configured; `settings` is empty in that case."""

    org_id: UUID
    plugin_id: str | None
    settings: dict


class VcsAuditPayload(BaseModel):
    plugin_id: str


async def get_vcs(session: AsyncSession, org_id: UUID) -> VcsState:
    row = (await session.execute(select(OrgRow).where(OrgRow.id == org_id))).scalar_one()
    return VcsState(
        org_id=org_id,
        plugin_id=row.vcs_plugin_id,
        settings=dict(row.vcs_settings or {}),
    )


async def set_vcs(
    session: AsyncSession,
    *,
    org_id: UUID,
    plugin_id: str,
    settings: dict,
    actor: Actor,
) -> VcsState:
    """Persist the chosen VCS plugin + its settings. Emits `vcs.installed`."""
    await session.execute(
        update(OrgRow).where(OrgRow.id == org_id).values(vcs_plugin_id=plugin_id, vcs_settings=settings)
    )
    await audit(
        "org",
        org_id,
        "vcs.installed",
        VcsAuditPayload(plugin_id=plugin_id),
        actor,
        org_id=org_id,
        session=session,
    )
    return VcsState(org_id=org_id, plugin_id=plugin_id, settings=settings)


async def clear_vcs(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor: Actor,
) -> bool:
    """Clear the org's VCS choice. Returns True if a row was modified. Emits
    `vcs.cleared` only when something was actually cleared."""
    row = (await session.execute(select(OrgRow).where(OrgRow.id == org_id))).scalar_one()
    if row.vcs_plugin_id is None:
        return False
    prior = row.vcs_plugin_id
    row.vcs_plugin_id = None
    row.vcs_settings = None
    await session.flush()
    await audit(
        "org",
        org_id,
        "vcs.cleared",
        VcsAuditPayload(plugin_id=prior),
        actor,
        org_id=org_id,
        session=session,
    )
    return True
