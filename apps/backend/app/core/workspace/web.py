"""HTTP wiring for `core/workspace`.

| Method | Path                                | Action                                                                    |
|--------|-------------------------------------|---------------------------------------------------------------------------|
| GET    | `/api/workspaces/connection_status` | `ORG_SETTINGS_READ` — aggregated heartbeat state for the current org. |

The connection-status banner polls every ~3s. Activity SSE is served by
`core/sse` at `/api/sse/workspace_activity/{id}` — see
[`core/sse`](../core_sse.md).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.agent_gateway import connection_status_for_org
from app.core.auth import Action, org_id_var
from app.core.database import session as db_session
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes

router = APIRouter()


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.get("/connection_status", dependencies=[Depends(require(Action.ORG_SETTINGS_READ))])
async def get_connection_status() -> dict[str, object]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        return await connection_status_for_org(org_id, session=s)


register_routes(RouteSpec(module_name="workspaces", router=router, url_prefix="/api/workspaces"))
