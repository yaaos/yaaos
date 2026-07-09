"""HTTP wiring for `domain/repos` — per-repo protected-code + auto-approve
config, and intake→pipeline trigger bindings.

| Method | Path                             | Action         |
|--------|----------------------------------|----------------|
| GET    | `/api/repos`                     | `REPOS_MANAGE` — accordion list: `vcs.list_installation_repos` joined against config rows |
| GET    | `/api/repos/config?repo=`        | `REPOS_MANAGE` — full config (settings + bindings across every intake point); always 200 — unconfigured is a state, not an error |
| PUT    | `/api/repos/settings?repo=`      | `REPOS_MANAGE` — protected mode + path sets + auto-approve, whole-section replace |
| POST   | `/api/repos/triggers?repo=`      | `REPOS_MANAGE` — add a trigger binding; 201 `{id}` |
| DELETE | `/api/repos/triggers/{binding_id}` | `REPOS_MANAGE` — remove a trigger binding; 204 / 404 |

Repos aren't entities — external ids from the VCS install. `repo` rides as a
query param (not a path segment) because external ids contain `/`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.core.auth import Action, org_id_var
from app.core.database import session as db_session
from app.core.intake import list_intake_points
from app.core.sessions import current_actor, require
from app.core.vcs import list_installation_repos, registered_plugin_ids
from app.core.webserver import RouteSpec, register_routes
from app.domain.repos import service as repos
from app.domain.repos.service import (
    BindingNotFoundError,
    DuplicateBindingError,
    InvalidCronError,
    InvalidProtectedGlobError,
    InvalidScheduleError,
    UnknownIntakePointError,
    UnknownPipelineError,
)
from app.domain.repos.types import (
    ProtectedMode,
    ProtectedPathSet,
    RepoSettingsSpec,
    TriggerBinding,
    TriggerBindingSpec,
)

router = APIRouter()


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


class RepoAccordionEntry(BaseModel):
    repo_external_id: str
    trigger_count: int
    has_protected_code: bool
    auto_approve_enabled: bool


class ListReposResponse(BaseModel):
    repos: list[RepoAccordionEntry]


@router.get("", dependencies=[Depends(require(Action.REPOS_MANAGE))])
async def list_repos_endpoint() -> ListReposResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")

    repo_ids: list[str] = []
    for plugin_id in registered_plugin_ids():
        repo_ids.extend(await list_installation_repos(plugin_id, org_id))

    async with db_session() as s:
        configs = {c.repo_external_id: c for c in await repos.list_repo_configs(org_id, session=s)}

    out = [
        RepoAccordionEntry(
            repo_external_id=repo_external_id,
            trigger_count=configs[repo_external_id].trigger_count if repo_external_id in configs else 0,
            has_protected_code=(
                configs[repo_external_id].has_protected_code if repo_external_id in configs else False
            ),
            auto_approve_enabled=(
                configs[repo_external_id].auto_approve_enabled if repo_external_id in configs else False
            ),
        )
        for repo_external_id in repo_ids
    ]
    return ListReposResponse(repos=out)


class RepoConfigResponse(BaseModel):
    repo_external_id: str
    protected_mode: ProtectedMode
    protected_path_sets: tuple[ProtectedPathSet, ...]
    auto_approve_enabled: bool
    auto_approve_conditions: dict[str, Any]
    bindings: list[TriggerBinding]


@router.get("/config", dependencies=[Depends(require(Action.REPOS_MANAGE))])
async def get_repo_config_endpoint(repo: str) -> RepoConfigResponse:
    """Always 200 — an absent settings row is the model's defaults, and
    `unconfigured` is a state the Repos page renders, not an error."""
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        settings = await repos.get_settings(org_id, repo, session=s)
        bindings: list[TriggerBinding] = []
        for point in list_intake_points():
            bindings.extend(await repos.find_bindings(org_id, repo, point.id, session=s))
    return RepoConfigResponse(
        repo_external_id=repo,
        protected_mode=settings.protected_mode,
        protected_path_sets=settings.protected_path_sets,
        auto_approve_enabled=settings.auto_approve_enabled,
        auto_approve_conditions=settings.auto_approve_conditions,
        bindings=bindings,
    )


@router.put("/settings", dependencies=[Depends(require(Action.REPOS_MANAGE))])
async def put_repo_settings_endpoint(repo: str, body: RepoSettingsSpec) -> Response:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        try:
            await repos.put_settings(org_id, repo, settings=body, actor=actor, session=s)
        except InvalidProtectedGlobError as exc:
            raise _err(400, "invalid_glob") from exc
        await s.commit()
    return Response(status_code=200)


class AddTriggerResponse(BaseModel):
    id: UUID


@router.post("/triggers", status_code=201, dependencies=[Depends(require(Action.REPOS_MANAGE))])
async def add_trigger_endpoint(repo: str, body: TriggerBindingSpec) -> AddTriggerResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        try:
            binding_id = await repos.add_binding(org_id, repo, spec=body, actor=actor, session=s)
        except UnknownIntakePointError as exc:
            raise _err(400, "unknown_point") from exc
        except InvalidCronError as exc:
            raise _err(400, "invalid_cron") from exc
        except InvalidScheduleError as exc:
            raise _err(400, "invalid_schedule") from exc
        except UnknownPipelineError as exc:
            raise _err(404, "pipeline_not_found") from exc
        except DuplicateBindingError as exc:
            raise _err(409, "duplicate_binding") from exc
        await s.commit()
    return AddTriggerResponse(id=binding_id)


@router.delete("/triggers/{binding_id}", dependencies=[Depends(require(Action.REPOS_MANAGE))])
async def remove_trigger_endpoint(binding_id: UUID) -> Response:
    actor = current_actor()
    async with db_session() as s:
        try:
            await repos.remove_binding(binding_id, actor=actor, session=s)
        except BindingNotFoundError as exc:
            raise _err(404, "not_found") from exc
        await s.commit()
    return Response(status_code=204)


register_routes(RouteSpec(module_name="repos", router=router, url_prefix="/api/repos"))
