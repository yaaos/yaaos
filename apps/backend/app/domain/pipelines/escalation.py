"""Escalation-target resolution for `domain/pipelines` — who gets notified
when a run needs a human. Used for run-terminal notification now; pause
escalation (once pauses exist) reuses the same resolution order.

Intra-module only — not re-exported from `__init__.py`. The engine
(`engine.py`) is the sole caller.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import ActorKind
from app.core.auth import Role
from app.core.identity import find_oauth_identity
from app.core.tenancy import list_memberships_for_org
from app.domain.pipelines.types import Kickoff

_ADMIN_ROLES = (Role.OWNER, Role.ADMIN)


async def resolve_escalation_targets(kickoff: Kickoff, org_id: UUID, *, session: AsyncSession) -> set[UUID]:
    """Resolution order: the kickoff actor when it's a yaaos user · the
    schedule's `notify_user_ids` · the webhook PR author's linked yaaos
    identity · falling back to the org's admins when nothing resolves — a
    run's terminal notification (or, later, a pause) must never wait on
    nobody."""
    if kickoff.actor.kind == ActorKind.USER and kickoff.actor.user_id is not None:
        return {kickoff.actor.user_id}
    if kickoff.notify_user_ids:
        return set(kickoff.notify_user_ids)
    if kickoff.actor.kind == ActorKind.GITHUB_USER and kickoff.actor.login is not None:
        identity = await find_oauth_identity(session, provider="github", external_subject=kickoff.actor.login)
        if identity is not None:
            return {identity.user_id}
    return await _org_admin_ids(org_id, session=session)


async def _org_admin_ids(org_id: UUID, *, session: AsyncSession) -> set[UUID]:
    memberships = await list_memberships_for_org(session, org_id)
    return {m.user_id for m in memberships if m.role in _ADMIN_ROLES}
