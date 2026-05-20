"""HTTP routes for memory CRUD."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.audit_log import Actor
from app.core.auth import public_route
from app.core.webserver import RouteSpec, register_routes
from app.domain.memory.service import (
    Lesson,
    LessonNotFoundError,
    LessonValidationError,
    create,
    delete,
    get,
    list_all,
    list_for_repo,
    update,
)

M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# M02 default-deny: legacy memory endpoints declare `public_route`.
router = APIRouter(dependencies=[Depends(public_route)])


class CreateLessonRequest(BaseModel):
    repo_external_id: str
    title: str
    body: str
    source_pr_url: str | None = None
    plugin_id: str = "github"


class UpdateLessonRequest(BaseModel):
    title: str | None = None
    body: str | None = None
    source_pr_url: str | None = None


@router.get("")
async def list_(repo_external_id: str | None = None) -> list[Lesson]:
    if repo_external_id is not None:
        return await list_for_repo(repo_external_id, org_id=M01_ORG_ID)
    return await list_all(org_id=M01_ORG_ID)


@router.post("")
async def create_lesson(req: CreateLessonRequest) -> Lesson:
    try:
        return await create(
            req.repo_external_id,
            req.title,
            req.body,
            req.source_pr_url,
            actor=Actor.system(),
            org_id=M01_ORG_ID,
            plugin_id=req.plugin_id,
        )
    except LessonValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{lesson_id}")
async def update_lesson(lesson_id: UUID, req: UpdateLessonRequest) -> Lesson:
    try:
        return await update(
            lesson_id,
            title=req.title,
            body=req.body,
            source_pr_url=req.source_pr_url,
            actor=Actor.system(),
            org_id=M01_ORG_ID,
        )
    except LessonNotFoundError:
        raise HTTPException(status_code=404, detail="lesson not found")
    except LessonValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{lesson_id}")
async def delete_lesson(lesson_id: UUID) -> dict[str, str]:
    try:
        await get(lesson_id, org_id=M01_ORG_ID)
    except LessonNotFoundError:
        raise HTTPException(status_code=404, detail="lesson not found")
    await delete(lesson_id, actor=Actor.system(), org_id=M01_ORG_ID)
    return {"status": "deleted"}


register_routes(RouteSpec(module_name="memory", router=router))
