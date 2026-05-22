"""HTTP wiring for per-org VCS install state.

| Method | Path           | Action      |
|--------|----------------|-------------|
| GET    | `/api/vcs`     | `VCS_READ`  — current VCS install (plugin_id + settings, never secrets) |
| POST   | `/api/vcs`     | `VCS_WRITE` — set chosen plugin + settings; returns either the new state OR an install URL when the plugin redirects |
| DELETE | `/api/vcs`     | `VCS_WRITE` — clear the org's VCS choice |

Org context comes from `X-Org-Slug` (per M02 pattern). Architecture.md writes
these as `/api/orgs/{slug}/vcs` for human readability; the actual implementation
mirrors the existing audit + memberships endpoints which take the slug via header.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth.context import org_id_var
from app.core.auth.types import Action
from app.core.database import session as db_session
from app.core.webserver import RouteSpec, register_routes
from app.domain.orgs import vcs as vcs_service
from app.domain.sessions.dependencies import current_actor, require
from app.domain.vcs import PluginNotFoundError, VCSValidationError
from app.domain.vcs import get_plugin as get_vcs_plugin

log = structlog.get_logger("orgs.vcs.web")

router = APIRouter()


class VcsStateResponse(BaseModel):
    plugin_id: str | None
    settings: dict


class SetVcsRequest(BaseModel):
    plugin_id: str
    settings: dict = {}


class SetVcsResponse(BaseModel):
    """Either the new state (when settings stored directly) OR an `install_url`
    the SPA should navigate to (when the plugin requires an out-of-band install)."""

    state: VcsStateResponse | None = None
    install_url: str | None = None


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


@router.get("", dependencies=[Depends(require(Action.VCS_READ))])
async def get_vcs_endpoint() -> VcsStateResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        state = await vcs_service.get_vcs(s, org_id)
    return VcsStateResponse(plugin_id=state.plugin_id, settings=state.settings)


@router.post("", dependencies=[Depends(require(Action.VCS_WRITE))])
async def set_vcs_endpoint(body: SetVcsRequest) -> SetVcsResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    try:
        plugin = get_vcs_plugin(body.plugin_id)
    except PluginNotFoundError as exc:
        raise _err(404, "plugin_not_found") from exc

    # Plugins with an install_url take precedence — the SPA navigates the
    # user to the plugin's install handshake (e.g. GitHub App) and the
    # handshake's callback writes `set_vcs(...)` itself.
    install_url = plugin.install_url(org_id)
    if install_url:
        return SetVcsResponse(install_url=install_url)

    try:
        validated = plugin.validate_settings(body.settings)
    except (VCSValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_settings", "message": str(exc)}
        ) from exc

    async with db_session() as s:
        state = await vcs_service.set_vcs(
            s,
            org_id=org_id,
            plugin_id=body.plugin_id,
            settings=validated,
            actor=actor,
        )
        await s.commit()
    return SetVcsResponse(state=VcsStateResponse(plugin_id=state.plugin_id, settings=state.settings))


@router.delete("", dependencies=[Depends(require(Action.VCS_WRITE))])
async def clear_vcs_endpoint() -> VcsStateResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        await vcs_service.clear_vcs(s, org_id=org_id, actor=actor)
        state = await vcs_service.get_vcs(s, org_id)
        await s.commit()
    return VcsStateResponse(plugin_id=state.plugin_id, settings=state.settings)


register_routes(RouteSpec(module_name="vcs", router=router, url_prefix="/api/vcs"))
