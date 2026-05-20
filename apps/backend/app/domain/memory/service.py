"""Per-repo lessons CRUD + retrieval."""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.core.audit_log import Actor, audit_for_lesson
from app.core.database import session as db_session
from app.domain.memory.models import LessonRow


class Lesson(BaseModel):
    id: UUID
    org_id: UUID
    plugin_id: str
    repo_external_id: str
    title: str
    body: str
    source_pr_url: str | None
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
            id=uuid4(),
            org_id=org_id,
            plugin_id=plugin_id,
            repo_external_id=repo_external_id,
            title=title,
            body=body,
            source_pr_url=source_pr_url,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        row_id = row.id

    await audit_for_lesson(
        row_id,
        "lesson.created",
        _LessonCreatedPayload(title=title, body_length=len(body)),
        actor=actor,
        org_id=org_id,
    )
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
        await s.commit()
        await s.refresh(row)

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
        )
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
        await s.commit()
    await audit_for_lesson(
        lesson_id,
        "lesson.deleted",
        _LessonDeletedPayload(title=title, body_hash_at_deletion=body_hash),
        actor=actor,
        org_id=org_id,
    )
