"""HTTP routes for lessons CRUD.

| Method | Path                  | Action          |
|--------|-----------------------|-----------------|
| GET    | `/api/lessons`        | `LESSONS_READ`  |
| GET    | `/api/lessons/{id}`   | `LESSONS_READ`  |
| POST   | `/api/lessons`        | `LESSONS_WRITE` |
| PUT    | `/api/lessons/{id}`   | `LESSONS_WRITE` |
| DELETE | `/api/lessons/{id}`   | `LESSONS_WRITE` |

Org context arrives via `X-Yaaos-Org-Slug` (RouteSecurity.ORG_SCOPED). Actor is the current
user, derived from the session cookie.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.auth import Action, org_id_var
from app.core.sessions import current_actor, require
from app.core.webserver import RouteSpec, register_routes
from app.domain.lessons.service import (
    Lesson,
    LessonFilter,
    LessonNotFoundError,
    LessonValidationError,
    create,
    delete,
    get,
    list_lessons,
    update,
)

router = APIRouter()


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


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _org() -> UUID:
    org_id = org_id_var.get()
    if org_id is None:
        raise _err(400, "no_org_context")
    return org_id


@router.get("", dependencies=[Depends(require(Action.LESSONS_READ))])
async def list_(
    repo_external_id: list[str] | None = Query(default=None),
    q: str | None = Query(default=None),
    created_by: UUID | None = Query(default=None),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    sort: str = Query(default="created_desc"),
    limit: int = Query(default=50, le=200),
) -> list[Lesson]:
    """list: q + repo multi + created_by + date range + sort.

    All filters are AND'd. `sort` accepts `created_desc` / `created_asc`
    / `updated_desc`; anything else falls back to `created_desc` in
    `LessonFilter`.
    """
    filt = LessonFilter(
        repo_external_ids=repo_external_id,
        q=q,
        created_by=created_by,
        created_after=created_after,
        created_before=created_before,
        sort=sort if sort in ("created_desc", "created_asc", "updated_desc") else "created_desc",  # type: ignore[arg-type]
    )
    return await list_lessons(filt, org_id=_org(), limit=limit)


@router.get("/{lesson_id}", dependencies=[Depends(require(Action.LESSONS_READ))])
async def get_lesson(lesson_id: UUID) -> Lesson:
    try:
        return await get(lesson_id, org_id=_org())
    except LessonNotFoundError:
        raise HTTPException(status_code=404, detail="lesson not found")


@router.post("", dependencies=[Depends(require(Action.LESSONS_WRITE))])
async def create_lesson(req: CreateLessonRequest) -> Lesson:
    try:
        return await create(
            req.repo_external_id,
            req.title,
            req.body,
            req.source_pr_url,
            actor=current_actor(),
            org_id=_org(),
            plugin_id=req.plugin_id,
        )
    except LessonValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{lesson_id}", dependencies=[Depends(require(Action.LESSONS_WRITE))])
async def update_lesson(lesson_id: UUID, req: UpdateLessonRequest) -> Lesson:
    try:
        return await update(
            lesson_id,
            title=req.title,
            body=req.body,
            source_pr_url=req.source_pr_url,
            actor=current_actor(),
            org_id=_org(),
        )
    except LessonNotFoundError:
        raise HTTPException(status_code=404, detail="lesson not found")
    except LessonValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{lesson_id}", dependencies=[Depends(require(Action.LESSONS_WRITE))])
async def delete_lesson(lesson_id: UUID) -> dict[str, str]:
    org_id = _org()
    try:
        await get(lesson_id, org_id=org_id)
    except LessonNotFoundError:
        raise HTTPException(status_code=404, detail="lesson not found")
    await delete(lesson_id, actor=current_actor(), org_id=org_id)
    return {"status": "deleted"}


register_routes(RouteSpec(module_name="lessons", router=router))
