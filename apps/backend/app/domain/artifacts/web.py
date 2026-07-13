"""HTTP wiring for `domain/artifacts` — read-only artifact surface.

| Method | Path                     | Action         |
|--------|--------------------------|-----------------|
| GET    | `/api/artifacts`         | `TICKETS_READ` — grouped-by-stage metadata for one ticket |
| GET    | `/api/artifacts/{id}`    | `TICKETS_READ` — one version, body included |

Read-only by design (see module docstring) — there is no write API here;
artifacts arrive only via the pipelines engine's `store`/`mark_final`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import Action, org_id_var
from app.core.database import session as db_session
from app.core.sessions import require
from app.core.webserver import RouteSpec, register_routes
from app.domain.artifacts import service as artifacts
from app.domain.artifacts.service import ArtifactNotFoundError
from app.domain.artifacts.types import ArtifactGroup

router = APIRouter()


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


class ListArtifactsResponse(BaseModel):
    artifacts: list[ArtifactGroup]


class ArtifactDetailResponse(BaseModel):
    id: UUID
    stage_name: str
    version: int
    iteration: int
    is_final: bool
    body: str
    run_id: UUID
    # Provenance: non-null iff this artifact was adopted from a ticket attachment.
    adopted_from_attachment_id: UUID | None
    created_at: datetime


@router.get("", dependencies=[Depends(require(Action.TICKETS_READ))])
async def list_artifacts_endpoint(ticket_id: UUID) -> ListArtifactsResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        groups = await artifacts.list_for_ticket(org_id, ticket_id, session=s)
    return ListArtifactsResponse(artifacts=groups)


@router.get("/{artifact_id}", dependencies=[Depends(require(Action.TICKETS_READ))])
async def get_artifact_endpoint(artifact_id: UUID) -> ArtifactDetailResponse:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    async with db_session() as s:
        try:
            artifact = await artifacts.get(artifact_id, org_id=org_id, session=s)
        except ArtifactNotFoundError as exc:
            raise _err(404, "not_found") from exc
    return ArtifactDetailResponse(
        id=artifact.id,
        stage_name=artifact.stage_name,
        version=artifact.version,
        iteration=artifact.iteration,
        is_final=artifact.is_final,
        body=artifact.body,
        run_id=artifact.run_id,
        adopted_from_attachment_id=artifact.adopted_from_attachment_id,
        created_at=artifact.created_at,
    )


register_routes(
    RouteSpec(
        module_name="artifacts",
        router=router,
        url_prefix="/api/artifacts",
    )
)
