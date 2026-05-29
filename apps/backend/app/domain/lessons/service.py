"""Per-repo lessons CRUD + retrieval."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

from app.core.audit_log import Actor, audit_for_lesson
from app.core.database import session as db_session
from app.domain.lessons.models import LessonRow


class Lesson(BaseModel):
    id: UUID
    org_id: UUID
    plugin_id: str
    repo_external_id: str
    title: str
    body: str
    source_pr_url: str | None
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: LessonRow) -> Lesson:
        return cls(
            id=row.id,
            org_id=row.org_id,
            plugin_id=row.plugin_id,
            repo_external_id=row.repo_external_id,
            title=row.title,
            body=row.body,
            source_pr_url=row.source_pr_url,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class LessonValidationError(ValueError):
    pass


class LessonNotFoundError(LookupError):
    pass


class _LessonCreatedPayload(BaseModel):
    title: str
    body_length: int


class _LessonUpdatedPayload(BaseModel):
    fields_changed: list[str]
    prior_body_hash: str
    new_body_hash: str


class _LessonDeletedPayload(BaseModel):
    title: str
    body_hash_at_deletion: str


def _validate(title: str, body: str) -> None:
    if not title or not title.strip():
        raise LessonValidationError("title is required")
    if len(title) > 200:
        raise LessonValidationError("title must be ≤200 chars")
    if not body or not body.strip():
        raise LessonValidationError("body is required")
    if len(body) > 1000:
        raise LessonValidationError("body must be ≤1000 chars")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


async def create(
    repo_external_id: str,
    title: str,
    body: str,
    source_pr_url: str | None,
    *,
    actor: Actor,
    org_id: UUID,
    plugin_id: str = "github",
) -> Lesson:
    _validate(title, body)
    if not repo_external_id.strip():
        raise LessonValidationError("repo_external_id is required")
    async with db_session() as s:
        row = LessonRow(
            org_id=org_id,
            plugin_id=plugin_id,
            repo_external_id=repo_external_id,
            title=title,
            body=body,
            source_pr_url=source_pr_url,
            # User-typed lessons get attribution; reviewer/agent-created
            # lessons leave actor.user_id None and the column stays NULL.
            created_by=actor.user_id,
        )
        s.add(row)
        await s.flush()
        row_id = row.id
        await audit_for_lesson(
            row_id,
            "lesson.created",
            _LessonCreatedPayload(title=title, body_length=len(body)),
            actor=actor,
            org_id=org_id,
            session=s,
        )
        await s.commit()
    return await get(row_id, org_id=org_id)


async def list_for_repo(repo_external_id: str, *, org_id: UUID, plugin_id: str = "github") -> list[Lesson]:
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(LessonRow)
                    .where(
                        LessonRow.org_id == org_id,
                        LessonRow.plugin_id == plugin_id,
                        LessonRow.repo_external_id == repo_external_id,
                    )
                    .order_by(LessonRow.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [Lesson.from_row(r) for r in rows]


class LessonFilter(BaseModel):
    """Query parameters for `list_lessons`. All fields optional."""

    repo_external_ids: list[str] | None = None
    q: str | None = None  # case-insensitive substring against title + body
    created_by: UUID | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    sort: Literal["created_desc", "created_asc", "updated_desc"] = "created_desc"


async def list_lessons(filter_: LessonFilter, *, org_id: UUID, limit: int = 50) -> list[Lesson]:
    """q / repo multi / created_by / date range / sort."""
    async with db_session() as s:
        stmt = select(LessonRow).where(LessonRow.org_id == org_id)
        if filter_.repo_external_ids:
            stmt = stmt.where(LessonRow.repo_external_id.in_(filter_.repo_external_ids))
        if filter_.q:
            term = f"%{filter_.q.lower()}%"
            stmt = stmt.where(func.lower(LessonRow.title).like(term) | func.lower(LessonRow.body).like(term))
        if filter_.created_by is not None:
            stmt = stmt.where(LessonRow.created_by == filter_.created_by)
        if filter_.created_after is not None:
            stmt = stmt.where(LessonRow.created_at >= filter_.created_after)
        if filter_.created_before is not None:
            stmt = stmt.where(LessonRow.created_at <= filter_.created_before)
        if filter_.sort == "created_asc":
            stmt = stmt.order_by(LessonRow.created_at.asc())
        elif filter_.sort == "updated_desc":
            stmt = stmt.order_by(LessonRow.updated_at.desc())
        else:
            stmt = stmt.order_by(LessonRow.created_at.desc())
        stmt = stmt.limit(limit)
        rows = (await s.execute(stmt)).scalars().all()
    return [Lesson.from_row(r) for r in rows]


async def list_all(*, org_id: UUID) -> list[Lesson]:
    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(LessonRow).where(LessonRow.org_id == org_id).order_by(LessonRow.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [Lesson.from_row(r) for r in rows]


async def get(lesson_id: UUID, *, org_id: UUID) -> Lesson:
    async with db_session() as s:
        row = (
            await s.execute(select(LessonRow).where(LessonRow.id == lesson_id, LessonRow.org_id == org_id))
        ).scalar_one_or_none()
    if row is None:
        raise LessonNotFoundError(str(lesson_id))
    return Lesson.from_row(row)


async def update(
    lesson_id: UUID,
    *,
    title: str | None = None,
    body: str | None = None,
    source_pr_url: str | None = None,
    actor: Actor,
    org_id: UUID,
) -> Lesson:
    async with db_session() as s:
        row = (
            await s.execute(select(LessonRow).where(LessonRow.id == lesson_id, LessonRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            raise LessonNotFoundError(str(lesson_id))
        new_title = title if title is not None else row.title
        new_body = body if body is not None else row.body
        _validate(new_title, new_body)
        prior_body_hash = _hash(row.body)
        changed: list[str] = []
        if title is not None and title != row.title:
            row.title = title
            changed.append("title")
        if body is not None and body != row.body:
            row.body = body
            changed.append("body")
        if source_pr_url is not None and source_pr_url != row.source_pr_url:
            row.source_pr_url = source_pr_url
            changed.append("source_pr_url")
        if changed:
            await audit_for_lesson(
                lesson_id,
                "lesson.updated",
                _LessonUpdatedPayload(
                    fields_changed=changed,
                    prior_body_hash=prior_body_hash,
                    new_body_hash=_hash(new_body),
                ),
                actor=actor,
                org_id=org_id,
                session=s,
            )
        await s.commit()
    return await get(lesson_id, org_id=org_id)


async def delete(lesson_id: UUID, *, actor: Actor, org_id: UUID) -> None:
    async with db_session() as s:
        row = (
            await s.execute(select(LessonRow).where(LessonRow.id == lesson_id, LessonRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            return
        title = row.title
        body_hash = _hash(row.body)
        await s.execute(sql_delete(LessonRow).where(LessonRow.id == lesson_id))
        await audit_for_lesson(
            lesson_id,
            "lesson.deleted",
            _LessonDeletedPayload(title=title, body_hash_at_deletion=body_hash),
            actor=actor,
            org_id=org_id,
            session=s,
        )
        await s.commit()
