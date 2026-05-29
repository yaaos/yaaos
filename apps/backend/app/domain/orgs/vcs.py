"""Per-org VCS plugin install state.

One VCS per org. State lives on the `orgs` table (`vcs_plugin_id`,
`vcs_settings`). Switching is explicit: clear, then set. Every mutation emits
an audit-log entry.

VCS plugins that own per-org install rows register a cleanup callback via
`register_vcs_clear_hook`. `clear_vcs` calls every registered hook so
plugin-owned data is wiped without `domain/orgs` importing plugin models.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.tenancy.models import OrgRow

log = structlog.get_logger("orgs.vcs")

# VcsClearHook: called by `clear_vcs` when an org's VCS choice is removed.
# Args: (org_id, plugin_id, session). Must flush inside the same transaction.
VcsClearHook = Callable[[UUID, str, AsyncSession], Awaitable[None]]

_VCS_CLEAR_HOOKS: list[VcsClearHook] = []


def register_vcs_clear_hook(hook: VcsClearHook) -> None:
    """Register a cleanup callback invoked by `clear_vcs`.

    Called by VCS plugins at boot so they can delete their per-org install
    rows without this module importing plugin models.
    """
    _VCS_CLEAR_HOOKS.append(hook)


def _reset_vcs_clear_hooks_for_tests() -> None:
    _VCS_CLEAR_HOOKS.clear()


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
    `vcs.cleared` only when something was actually cleared.

    Also wipes plugin-owned credentials/install rows so Remove means "fully
    disconnected" — the next Add starts from a blank slate. has one VCS
    plugin (github), so the cleanup is inlined here rather than dispatched
    through a plugin hook; revisit when a second VCS plugin ships.
    """
    row = (await session.execute(select(OrgRow).where(OrgRow.id == org_id))).scalar_one()
    if row.vcs_plugin_id is None:
        return False
    prior = row.vcs_plugin_id
    row.vcs_plugin_id = None
    row.vcs_settings = None
    # Call registered VCS plugin cleanup hooks (e.g. deleting github install
    # rows). Plugins register via `register_vcs_clear_hook` at boot; no
    # direct plugin-model import needed here.
    for hook in _VCS_CLEAR_HOOKS:
        await hook(org_id, prior, session)
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
