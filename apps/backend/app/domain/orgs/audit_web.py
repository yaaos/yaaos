"""HTTP wiring for `/api/audit` — org-scoped read of the audit log.

Lives under `domain/orgs` because it's an org-admin view tied to the current
`X-Org-Slug`. Owners/Admins (`Action.AUDIT_READ` → Admin minimum) can list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.audit_log import list_for_org
from app.core.auth.context import org_id_var
from app.core.auth.types import Action
from app.core.webserver import RouteSpec, register_routes
from app.domain.sessions.dependencies import require

router = APIRouter()


class AuditEntryView(BaseModel):
    id: UUID
    entity_kind: str
    entity_id: UUID
    kind: str
    payload: dict
    actor_kind: str
    actor_user_id: UUID | None
    actor_login: str | None
    created_at: str


@router.get("", dependencies=[Depends(require(Action.AUDIT_READ))])
async def list_audit(
    actor_kind: Annotated[list[str] | None, Query()] = None,
    action: Annotated[list[str] | None, Query()] = None,
    before_ts: Annotated[datetime | None, Query()] = None,
    after_ts: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[AuditEntryView]:
    org_id = org_id_var.get()
    assert org_id is not None  # require() guarantees this
    rows = await list_for_org(
        org_id=org_id,
        actor_kinds=actor_kind,
        actions=action,
        before_ts=before_ts,
        after_ts=after_ts,
        limit=limit,
    )
    return [
        AuditEntryView(
            id=r.id,
            entity_kind=r.entity_kind,
            entity_id=r.entity_id,
            kind=r.kind,
            payload=r.payload,
            actor_kind=r.actor.kind.value,
            actor_user_id=r.actor.user_id,
            actor_login=r.actor.login,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


register_routes(RouteSpec(module_name="audit", router=router, url_prefix="/api/audit"))


__all__ = ["router"]
