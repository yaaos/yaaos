"""HTTP wiring for per-org coding-agent installs.

| Method | Path                                  | Action                |
|--------|---------------------------------------|-----------------------|
| GET    | `/api/coding-agents`                  | `CODING_AGENT_READ`   — list installs (with per-plugin stage_options) |
| GET    | `/api/coding-agents/available`        | `CODING_AGENT_READ`   — list registered plugins (for install picker) |
| POST   | `/api/coding-agents`                  | `CODING_AGENT_WRITE`  — install a plugin |
| PATCH  | `/api/coding-agents/{plugin_id}`      | `CODING_AGENT_WRITE`  — replace settings |
| DELETE | `/api/coding-agents/{plugin_id}`      | `CODING_AGENT_WRITE`  — uninstall |

Org context via `X-Yaaos-Org-Slug` header (RouteSecurity.ORG_SCOPED).
"""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import app.core.coding_agent.installs as ca_service
from app.core.auth import Action, org_id_var, user_id_var
from app.core.coding_agent.service import get_plugin as get_coding_agent_plugin
from app.core.coding_agent.service import list_plugins as list_coding_agent_plugins
from app.core.coding_agent.skills_bundle import build_skills_bundle_zip
from app.core.coding_agent.types import PluginNotFoundError
from app.core.database import session as db_session
from app.core.sessions import current_actor, require
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("coding_agent.installs.web")

router = APIRouter()


class CodingAgentView(BaseModel):
    """Per-org coding-agent install row, enriched with per-plugin display metadata.

    `display_name`, `models`, and `efforts` are read from the registered plugin
    instance at request time — they reflect the plugin's current defaults, not
    persisted values.
    """

    plugin_id: str
    settings: dict
    created_at: datetime
    updated_at: datetime
    display_name: str
    models: list[str]
    efforts: list[str]


class AvailablePluginView(BaseModel):
    """Thin summary of a registered coding-agent plugin for the install picker."""

    plugin_id: str
    display_name: str


class InstallRequest(BaseModel):
    plugin_id: str
    settings: dict = {}


class UpdateSettingsRequest(BaseModel):
    settings: dict


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _view(install: ca_service.CodingAgentInstall) -> CodingAgentView:
    try:
        plugin = get_coding_agent_plugin(install.plugin_id)
        opts = plugin.stage_options()
        display_name = plugin.display_name
        models: list[str] = list(opts.models)
        efforts: list[str] = list(opts.efforts)
    except PluginNotFoundError:
        # Plugin was unregistered since install — surface empty lists so the
        # row is still renderable rather than raising a 500 on a list call.
        display_name = install.plugin_id
        models = []
        efforts = []

    return CodingAgentView(
        plugin_id=install.plugin_id,
        settings=install.settings,
        created_at=install.created_at,
        updated_at=install.updated_at,
        display_name=display_name,
        models=models,
        efforts=efforts,
    )


@router.get("", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def list_endpoint() -> list[CodingAgentView]:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        installs = await ca_service.list_coding_agents(s, org_id)
    return [_view(i) for i in installs]


@router.get("/available", dependencies=[Depends(require(Action.CODING_AGENT_READ))])
async def available_endpoint() -> dict[str, list[AvailablePluginView]]:
    """List all registered coding-agent plugins regardless of org install state.

    Used by the Coding Agents settings page to populate the install picker so
    admins can add a plugin that isn't yet installed in their org.
    """
    plugins = list_coding_agent_plugins()
    return {
        "plugins": [AvailablePluginView(plugin_id=p.plugin_id, display_name=p.display_name) for p in plugins]
    }


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


@router.get(
    "/{plugin_id}/skills-bundle",
    dependencies=[Depends(require(Action.CODING_AGENT_READ))],
    response_class=Response,
)
async def skills_bundle_endpoint(plugin_id: str) -> Response:
    """Return a vendor-native skills bundle ZIP for the given plugin.

    The ZIP is generated at request time from the canonical `.claude/` source
    tree baked into the backend image.  Entry paths are repo-root-relative so
    "unzip at the repo root" installs the skills.

    Errors:
        404 unknown_plugin — plugin_id not registered.
        500 skills_source_missing — source directory absent from the image
            (deploy defect).
    """
    try:
        data = await build_skills_bundle_zip(plugin_id)
    except PluginNotFoundError as exc:
        raise _err(404, "unknown_plugin") from exc
    except FileNotFoundError as exc:
        raise _err(500, "skills_source_missing") from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": (f'attachment; filename="yaaos-pipeline-skills-{plugin_id}.zip"')},
    )


register_routes(RouteSpec(module_name="coding_agent", router=router, url_prefix="/api/coding-agents"))
