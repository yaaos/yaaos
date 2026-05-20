"""HTTP wiring for top-level org settings.

| Method | Path        | Action               |
|--------|-------------|----------------------|
| PATCH  | `/api/orgs` | `ORG_SETTINGS_WRITE` — Owner/Admin can update `session_timeout_override` (and future top-level org fields). |

Org identified by `X-Org-Slug` header (M02 pattern). Architecture.md documents
the URL as `/api/orgs/{slug}` for readability; this implementation mirrors the
other M03 endpoints which all take the slug via header. The single endpoint
returns the updated org's relevant settings.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.auth.context import org_id_var
from app.core.auth.types import Action
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.domain.auth.dependencies import require
from app.domain.orgs.models import OrgRow

log = structlog.get_logger("orgs.settings.web")

router = APIRouter()


class _PatchOrgRequest(BaseModel):
    # Pydantic v2 allows the field to be absent OR explicitly null. Absent
    # = "don't touch"; null = "clear the override and fall back to the global
    # constant"; positive int = "set to N minutes".
    session_timeout_override: int | None = Field(default=None)
    _set_session_timeout_override: bool = False  # internal: did the client include the key?


class _OrgSettingsResponse(BaseModel):
    slug: str
    session_timeout_override: int | None


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.patch("", dependencies=[Depends(require(Action.ORG_SETTINGS_WRITE))])
async def patch_org_settings(body: dict) -> _OrgSettingsResponse:
    """Update top-level org settings. Body is a JSON object; only the keys
    actually present are touched. M03 supports `session_timeout_override`
    (null clears it, positive int sets minutes)."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")

    async with db_session() as s:
        row = (await s.execute(select(OrgRow).where(OrgRow.id == org_id))).scalar_one()
        if "session_timeout_override" in body:
            value = body["session_timeout_override"]
            if value is not None:
                if not isinstance(value, int) or value <= 0:
                    raise _err(422, "invalid_session_timeout_override")
            row.session_timeout_override = value
        await s.commit()
        await s.refresh(row)
    return _OrgSettingsResponse(slug=row.slug, session_timeout_override=row.session_timeout_override)


register_routes(RouteSpec(module_name="orgs", router=router, url_prefix="/api/orgs"))
