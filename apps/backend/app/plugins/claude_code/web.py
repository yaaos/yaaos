"""HTTP routes owned by the claude_code plugin.

Plugin-owned URL namespace: settings under `/api/claude_code/...`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import Action
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes

# Default-deny: each route declares `require(action)` (settings UI endpoints).
router = APIRouter()


@router.get("/defaults", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def defaults_endpoint() -> dict:
    """Model / effort dropdown enums for the Claude Code settings UI.
    Imported at request time so a code change surfaces on the next request."""
    from app.plugins.claude_code.defaults import EFFORTS, MODELS  # noqa: PLC0415

    return {"models": list(MODELS), "efforts": list(EFFORTS)}


register_routes(RouteSpec(module_name="claude_code", router=router))
