"""HTTP routes owned by the claude_code plugin.

Plugin-owned URL namespace per `plan/milestones/M01-code-review/backend.md` §
2026-05-16 — each plugin's credential setter and health-check endpoint live
under `/api/<plugin>/...`, not under a generic `/api/settings/...`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import public_route
from app.core.auth.types import Action
from app.core.webserver import RouteSpec, register_routes
from app.domain.sessions.dependencies import require
from app.plugins.claude_code.service import _set_anthropic_key, bootstrap_anthropic_env, get_plugin

M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# M02 default-deny: each route declares either `public_route` (M01-era
# unscoped setup endpoints) or `require(action)` (M03 settings UI endpoints).
router = APIRouter()


class SetApiKeyRequest(BaseModel):
    api_key: str


@router.post("/api_key", dependencies=[Depends(public_route)])
async def set_api_key(req: SetApiKeyRequest) -> dict[str, str]:
    if not req.api_key.strip():
        raise HTTPException(status_code=400, detail={"api_key": "must not be empty"})
    await _set_anthropic_key(M01_ORG_ID, req.api_key)
    return {"status": "saved"}


@router.get("/health", dependencies=[Depends(public_route)])
async def health() -> dict[str, object]:
    h = await get_plugin().health_check()
    return {"healthy": h.healthy, "message": h.message, "checked_at": h.checked_at}


@router.get("/defaults", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def defaults_endpoint() -> dict:
    """Code defaults for the orchestrator + sub-agents, plus the model /
    version / effort dropdown enums. Imported at request time so a code
    change to `defaults.py` surfaces on the next request — never cached."""
    from app.plugins.claude_code.defaults import get_defaults  # noqa: PLC0415

    return get_defaults()


register_routes(
    RouteSpec(
        module_name="claude_code",
        router=router,
        on_startup=[bootstrap_anthropic_env],
    )
)
