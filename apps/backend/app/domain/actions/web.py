"""HTTP wiring for `domain/actions` — the Pipelines-page "Add an action"
picker.

| Method | Path          | Action            |
|--------|---------------|-------------------|
| GET    | `/api/actions`| `PIPELINES_MANAGE` — every registered action, `{action_id, plugin_id, label}` |

Read-only — actions register themselves at plugin-bootstrap time; there is
no write API here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import Action
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.domain.actions.registry import list_actions
from app.domain.actions.types import ActionInfo

router = APIRouter()


class ListActionsResponse(BaseModel):
    actions: list[ActionInfo]


@router.get("", dependencies=[Depends(require(Action.PIPELINES_MANAGE))])
async def list_actions_endpoint() -> ListActionsResponse:
    return ListActionsResponse(actions=list_actions())


register_routes(RouteSpec(module_name="actions", router=router, url_prefix="/api/actions"))
