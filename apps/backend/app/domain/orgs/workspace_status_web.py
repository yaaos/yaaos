"""HTTP wiring for the workspace-agent connection status (M05 Phase 7).

| Method | Path                                | Action               |
|--------|-------------------------------------|----------------------|
| GET    | `/api/workspaces/connection_status` | `ORG_SETTINGS_READ` — aggregated heartbeat state for the current org. |

Returns `{state, pod_count, latest_heartbeat_at}`. `state` is one of:

- `not_configured` — no agent pod has ever exchanged identity for this org
- `lost` — pods exist but none heartbeated within the last 90s
- `connected` — at least one pod heartbeated within 90s

The UI polls this every ~3s; long-poll / SSE not needed at the cadence and
granularity the status banner cares about.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.agent_gateway.service import connection_status_for_org
from app.core.auth.context import org_id_var
from app.core.auth.types import Action
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.domain.sessions.dependencies import require

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
