"""HTTP wiring for `domain/pipelines` — pipeline-definition CRUD.

| Method | Path                  | Action              |
|--------|-----------------------|----------------------|
| GET    | `/api/pipelines`      | `PIPELINES_MANAGE` — list org pipeline definitions |
| POST   | `/api/pipelines`      | `PIPELINES_MANAGE` — create |
| GET    | `/api/pipelines/{id}` | `PIPELINES_MANAGE` — read one |
| PUT    | `/api/pipelines/{id}` | `PIPELINES_MANAGE` — replace definition (applies to new runs only) |
| DELETE | `/api/pipelines/{id}` | `PIPELINES_MANAGE` — delete; 409 if referenced |
| POST   | `/api/pipelines/runs/{run_id}/cancel` | `REVIEWER_WRITE` — cancel a run; running cancels at the next boundary, queued cancels immediately, terminal 409s |
| POST   | `/api/pipelines/runs/pauses/{pause_id}/respond` | `REVIEWER_WRITE` — resolve a HITL pause; responders = the pause's escalation set union org admins (`403 not_escalation_target` otherwise) |

Further run-lifecycle endpoints (rerun, run/overview reads) land with the rest of the run engine.

Request bodies for create/update are the `PipelineDefinition` model itself:
`id` (top-level and per-stage) defaults to a fresh uuid7 at parse time, so a
client omitting `id` on a new pipeline or a newly-added stage gets one
server-minted for free — no separate "create request" shape needed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.core.auth import Action, org_id_var
from app.core.database import session as db_session
from app.core.sessions import current_actor, require
from app.core.webserver import RouteSpec, register_routes
from app.domain.pipelines import service as pipelines
from app.domain.pipelines.definition import PipelineDefinition, PipelineValidationError, Stage
from app.domain.pipelines.service import (
    NotEscalationTargetError,
    PauseAlreadyResolvedError,
    PauseNotFoundError,
    PipelineNameTakenError,
    PipelineNotFoundError,
    PipelineReferencedError,
    RunAlreadyTerminalError,
    RunNotFoundError,
)
from app.domain.pipelines.types import PauseResolution, Pipeline, PipelineSummary

router = APIRouter()


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


class ListPipelinesResponse(BaseModel):
    pipelines: list[PipelineSummary]


class CreatePipelineResponse(BaseModel):
    id: UUID


class PipelineDetailResponse(BaseModel):
    """Flat wire shape — `Pipeline.definition`'s fields ride at the top
    level alongside the stored-entity metadata."""

    id: UUID
    name: str
    description: str
    stages: tuple[Stage, ...]
    updated_at: datetime
    updated_by_login: str | None
    referenced: bool

    @classmethod
    def from_pipeline(cls, pipeline: Pipeline) -> PipelineDetailResponse:
        return cls(
            id=pipeline.definition.id,
            name=pipeline.definition.name,
            description=pipeline.definition.description,
            stages=pipeline.definition.stages,
            updated_at=pipeline.updated_at,
            updated_by_login=pipeline.updated_by_login,
            referenced=pipeline.referenced,
        )


@router.get("", dependencies=[Depends(require(Action.PIPELINES_MANAGE))])
async def list_pipelines_endpoint() -> ListPipelinesResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        summaries = await pipelines.list_pipelines(org_id, session=s)
    return ListPipelinesResponse(pipelines=summaries)


@router.post("", status_code=201, dependencies=[Depends(require(Action.PIPELINES_MANAGE))])
async def create_pipeline_endpoint(definition: PipelineDefinition) -> CreatePipelineResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    actor = current_actor()
    async with db_session() as s:
        try:
            pipeline_id = await pipelines.create_pipeline(
                org_id=org_id, definition=definition, actor=actor, session=s
            )
        except PipelineValidationError as exc:
            raise _err(400, "invalid_definition") from exc
        except PipelineNameTakenError as exc:
            raise _err(409, "name_taken") from exc
        await s.commit()
    return CreatePipelineResponse(id=pipeline_id)


@router.get("/{pipeline_id}", dependencies=[Depends(require(Action.PIPELINES_MANAGE))])
async def get_pipeline_endpoint(pipeline_id: UUID) -> PipelineDetailResponse:
    async with db_session() as s:
        try:
            pipeline = await pipelines.get_pipeline(pipeline_id, session=s)
        except PipelineNotFoundError as exc:
            raise _err(404, "not_found") from exc
    return PipelineDetailResponse.from_pipeline(pipeline)


@router.put("/{pipeline_id}", dependencies=[Depends(require(Action.PIPELINES_MANAGE))])
async def update_pipeline_endpoint(
    pipeline_id: UUID, definition: PipelineDefinition
) -> PipelineDetailResponse:
    actor = current_actor()
    async with db_session() as s:
        try:
            await pipelines.update_pipeline(pipeline_id, definition=definition, actor=actor, session=s)
        except PipelineNotFoundError as exc:
            raise _err(404, "not_found") from exc
        except PipelineValidationError as exc:
            raise _err(400, "invalid_definition") from exc
        except PipelineNameTakenError as exc:
            raise _err(409, "name_taken") from exc
        await s.commit()
        pipeline = await pipelines.get_pipeline(pipeline_id, session=s)
    return PipelineDetailResponse.from_pipeline(pipeline)


@router.delete("/{pipeline_id}", dependencies=[Depends(require(Action.PIPELINES_MANAGE))])
async def delete_pipeline_endpoint(pipeline_id: UUID) -> Response:
    actor = current_actor()
    async with db_session() as s:
        try:
            await pipelines.delete_pipeline(pipeline_id, actor=actor, session=s)
        except PipelineNotFoundError as exc:
            raise _err(404, "not_found") from exc
        except PipelineReferencedError as exc:
            raise _err(409, "referenced") from exc
        await s.commit()
    return Response(status_code=204)


@router.post("/runs/{run_id}/cancel", status_code=202, dependencies=[Depends(require(Action.REVIEWER_WRITE))])
async def cancel_run_endpoint(run_id: UUID) -> Response:
    actor = current_actor()
    async with db_session() as s:
        try:
            await pipelines.request_cancel(run_id, actor=actor, session=s)
        except RunNotFoundError as exc:
            raise _err(404, "not_found") from exc
        except RunAlreadyTerminalError as exc:
            raise _err(409, "terminal") from exc
        await s.commit()
    return Response(status_code=202)


class RespondPauseResponse(BaseModel):
    run_state: str


@router.post("/runs/pauses/{pause_id}/respond", dependencies=[Depends(require(Action.REVIEWER_WRITE))])
async def respond_pause_endpoint(pause_id: UUID, resolution: PauseResolution) -> RespondPauseResponse:
    actor = current_actor()
    async with db_session() as s:
        try:
            await pipelines.resolve_pause(pause_id, resolution=resolution, actor=actor, session=s)
        except PauseNotFoundError as exc:
            raise _err(404, "not_found") from exc
        except NotEscalationTargetError as exc:
            raise _err(403, "not_escalation_target") from exc
        except PauseAlreadyResolvedError as exc:
            raise _err(409, "already_resolved") from exc
        except NotImplementedError as exc:
            raise _err(400, "invalid_resolution") from exc
        await s.commit()
        run_state = await pipelines.get_run_state_for_pause(pause_id, session=s)
    return RespondPauseResponse(run_state=run_state)


register_routes(
    RouteSpec(
        module_name="pipelines",
        router=router,
        url_prefix="/api/pipelines",
    )
)
