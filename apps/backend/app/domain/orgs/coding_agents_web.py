"""HTTP wiring for per-org coding-agent installs.

| Method | Path                                  | Action                |
|--------|---------------------------------------|-----------------------|
| GET    | `/api/coding-agents`                  | `CODING_AGENT_READ`   — list installs |
| POST   | `/api/coding-agents`                  | `CODING_AGENT_WRITE`  — install a plugin |
| PATCH  | `/api/coding-agents/{plugin_id}`      | `CODING_AGENT_WRITE`  — replace settings |
| DELETE | `/api/coding-agents/{plugin_id}`      | `CODING_AGENT_WRITE`  — uninstall |

Org context via `X-Org-Slug` header (M02 pattern).
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth.context import org_id_var, user_id_var
from app.core.auth.types import Action
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.domain.coding_agent import PluginNotFoundError
from app.domain.coding_agent import get_plugin as get_coding_agent_plugin
from app.domain.orgs import coding_agents as ca_service
from app.domain.sessions.dependencies import current_actor, require

log = structlog.get_logger("orgs.coding_agents.web")

router = APIRouter()


class CodingAgentView(BaseModel):
    plugin_id: str
    settings: dict
    created_at: datetime
    updated_at: datetime


class InstallRequest(BaseModel):
    plugin_id: str
    settings: dict = {}


class UpdateSettingsRequest(BaseModel):
    settings: dict


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _view(install: ca_service.CodingAgentInstall) -> CodingAgentView:
    return CodingAgentView(
        plugin_id=install.plugin_id,
        settings=install.settings,
        created_at=install.created_at,
        updated_at=install.updated_at,
    )


@router.get("", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def list_endpoint() -> list[CodingAgentView]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        installs = await ca_service.list_coding_agents(s, org_id)
    return [_view(i) for i in installs]


@router.post("", dependencies=[Depends(require(Action.CODING_AGENT_WRITE))])
async def install_endpoint(body: InstallRequest) -> CodingAgentView:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    try:
        plugin = get_coding_agent_plugin(body.plugin_id)
    except PluginNotFoundError as exc:
        raise _err(404, "plugin_not_found") from exc
    try:
        validated = plugin.validate_settings(body.settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_settings", "message": str(exc)}
        ) from exc

    created_by = user_id_var.get()
    async with db_session() as s:
        try:
            install = await ca_service.install_coding_agent(
                s,
                org_id=org_id,
                plugin_id=body.plugin_id,
                settings=validated,
                actor=actor,
                created_by=created_by,
            )
        except ca_service.CodingAgentAlreadyInstalledError as exc:
            raise _err(409, "already_installed") from exc
        await s.commit()
    return _view(install)


@router.patch("/{plugin_id}", dependencies=[Depends(require(Action.CODING_AGENT_WRITE))])
async def update_settings_endpoint(plugin_id: str, body: UpdateSettingsRequest) -> CodingAgentView:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    try:
        plugin = get_coding_agent_plugin(plugin_id)
    except PluginNotFoundError as exc:
        raise _err(404, "plugin_not_found") from exc
    try:
        validated = plugin.validate_settings(body.settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_settings", "message": str(exc)}
        ) from exc

    async with db_session() as s:
        try:
            install = await ca_service.update_coding_agent_settings(
                s, org_id=org_id, plugin_id=plugin_id, settings=validated, actor=actor
            )
        except ca_service.CodingAgentNotInstalledError as exc:
            raise _err(404, "not_installed") from exc
        await s.commit()
    return _view(install)


@router.delete("/{plugin_id}", dependencies=[Depends(require(Action.CODING_AGENT_WRITE))])
async def uninstall_endpoint(plugin_id: str) -> dict[str, bool]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        removed = await ca_service.uninstall_coding_agent(s, org_id=org_id, plugin_id=plugin_id, actor=actor)
        await s.commit()
    if not removed:
        raise _err(404, "not_installed")
    return {"removed": True}


register_routes(RouteSpec(module_name="coding_agents", router=router, url_prefix="/api/coding-agents"))
