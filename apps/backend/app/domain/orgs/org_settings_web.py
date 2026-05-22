"""HTTP wiring for top-level org settings.

| Method | Path        | Action               |
|--------|-------------|----------------------|
| GET    | `/api/orgs` | `ORG_SETTINGS_READ` — return current top-level settings (`session_timeout_override`, `workspace_provider`, `registered_iam_arn`). |
| PATCH  | `/api/orgs` | `ORG_SETTINGS_WRITE` — Owner/Admin can update `session_timeout_override`, `workspace_provider`, `registered_iam_arn`. |

Org identified by `X-Org-Slug` header (M02 pattern). Architecture.md documents
the URL as `/api/orgs/{slug}` for readability; this implementation mirrors the
other M03 endpoints which all take the slug via header. The single endpoint
returns the updated org's relevant settings.

`workspace_provider` is `in_memory` or `remote_agent`. When set to
`remote_agent`, `registered_iam_arn` must also be set — the identity-exchange
verifier matches the agent's signed STS payload against this ARN.
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
from app.domain.orgs.models import OrgRow
from app.domain.sessions.dependencies import require

log = structlog.get_logger("orgs.settings.web")

router = APIRouter()


class _PatchOrgRequest(BaseModel):
    # Pydantic v2 allows the field to be absent OR explicitly null. Absent
    # = "don't touch"; null = "clear the override and fall back to the global
    # constant"; positive int = "set to N minutes".
    session_timeout_override: int | None = Field(default=None)
    _set_session_timeout_override: bool = False  # internal: did the client include the key?


_ALLOWED_WORKSPACE_PROVIDERS = {"in_memory", "remote_agent"}


class _OrgSettingsResponse(BaseModel):
    slug: str
    session_timeout_override: int | None
    workspace_provider: str | None = None
    registered_iam_arn: str | None = None


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.get("", dependencies=[Depends(require(Action.ORG_SETTINGS_READ))])
async def get_org_settings() -> _OrgSettingsResponse:
    """Return the current org's top-level settings. Lets the SPA's Settings
    page show what's actually set before the user edits."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        row = (await s.execute(select(OrgRow).where(OrgRow.id == org_id))).scalar_one()
    return _OrgSettingsResponse(
        slug=row.slug,
        session_timeout_override=row.session_timeout_override,
        workspace_provider=row.workspace_provider,
        registered_iam_arn=row.registered_iam_arn,
    )


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
        if "workspace_provider" in body:
            value = body["workspace_provider"]
            if value is not None and value not in _ALLOWED_WORKSPACE_PROVIDERS:
                raise _err(422, "invalid_workspace_provider")
            row.workspace_provider = value
        if "registered_iam_arn" in body:
            value = body["registered_iam_arn"]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise _err(422, "invalid_registered_iam_arn")
            row.registered_iam_arn = value
        # Cross-field: remote_agent provider requires an ARN.
        if row.workspace_provider == "remote_agent" and not row.registered_iam_arn:
            raise _err(422, "remote_agent_requires_iam_arn")
        await s.commit()
        await s.refresh(row)
    return _OrgSettingsResponse(
        slug=row.slug,
        session_timeout_override=row.session_timeout_override,
        workspace_provider=row.workspace_provider,
        registered_iam_arn=row.registered_iam_arn,
    )


register_routes(RouteSpec(module_name="orgs", router=router, url_prefix="/api/orgs"))
