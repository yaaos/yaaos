"""HTTP wiring for `domain/plugins` — picker enumeration endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.core.auth import Action
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.domain.plugins.service import list_available


class PluginMetaPayload(BaseModel):
    id: str
    type: str
    display_name: str
    description: str | None = None
    docs_url: str | None = None


class ListAvailableResponse(BaseModel):
    plugins: list[PluginMetaPayload]


router = APIRouter()


@router.get("/available", dependencies=[Depends(require(Action.MEMBERS_READ))])
async def list_available_endpoint(
    type: Literal["vcs", "coding_agent"] = Query(..., description="Plugin type to enumerate"),
) -> ListAvailableResponse:
    """Enumerate registered plugins of the requested type. The settings UI
    consumes this for the VCS + Coding Agents pickers — no plugin id is
    hardcoded in the frontend."""
    metas = list_available(type)
    return ListAvailableResponse(
        plugins=[
            PluginMetaPayload(
                id=m.id,
                type=m.type,
                display_name=m.display_name,
                description=m.description,
                docs_url=m.docs_url,
            )
            for m in metas
        ]
    )


register_routes(RouteSpec(module_name="plugins", router=router, url_prefix="/api/plugins"))
