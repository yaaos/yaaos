"""Service layer for `core/notifications`.

Write path:
    `create(*, user_id, org_id, type, title, body, subject_type, subject_id, session)`
    — idempotent by `(user_id, type, subject_type, subject_id)` when a subject
    is provided; subject-less notifications are always written.

Read paths:
    `list_for_user(...)` — full list (filterable by read_state, org, types).
    `popover_for_user(...)` — trimmed peek for the sidebar bell.
    `mark_read(...)` / `mark_all_read(...)` — idempotent state transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.notifications.models import NotificationRow

ReadState = Literal["all", "unread", "read"]


class Notification(BaseModel):
    id: UUID
    user_id: UUID
    org_id: UUID
    type: str
    subject_type: str | None
    subject_id: UUID | None
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: NotificationRow) -> Notification:
        return cls(
            id=row.id,
            user_id=row.user_id,
            org_id=row.org_id,
            type=row.type,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            title=row.title,
            body=row.body,
            read_at=row.read_at,
            created_at=row.created_at,
        )


async def create(
    *,
    user_id: UUID,
    org_id: UUID,
    type: str,
    title: str,
    body: str,
    subject_type: str | None = None,
    subject_id: UUID | None = None,
    session: AsyncSession,
) -> Notification | None:
    """Idempotent write.

    When a subject is provided, keyed on `(user_id, type, subject_type, subject_id)` —
    re-emitting the same event for the same subject is a no-op, returning `None`.
    Subject-less notifications (both null) are always written.

    Invariant: `subject_type` and `subject_id` must be both null or both set.
    """
    if (subject_type is None) != (subject_id is None):
        raise ValueError("subject_type and subject_id must both be null or both be set")

    if subject_type is not None:
        existing = (
            await session.execute(
                select(NotificationRow).where(
                    NotificationRow.user_id == user_id,
                    NotificationRow.type == type,
                    NotificationRow.subject_type == subject_type,
                    NotificationRow.subject_id == subject_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

    row = NotificationRow(
        user_id=user_id,
        org_id=org_id,
        type=type,
        title=title,
        body=body,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    session.add(row)
    await session.flush()
    return Notification.from_row(row)


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: UUID,
    read_state: ReadState = "all",
    org_id: UUID | None = None,
    types: list[str] | None = None,
    limit: int = 50,
) -> list[Notification]:
    stmt = (
        select(NotificationRow)
        .where(NotificationRow.user_id == user_id)
        .order_by(desc(NotificationRow.created_at))
        .limit(limit)
    )
    if read_state == "unread":
        stmt = stmt.where(NotificationRow.read_at.is_(None))
    elif read_state == "read":
        stmt = stmt.where(NotificationRow.read_at.is_not(None))
    if org_id is not None:
        stmt = stmt.where(NotificationRow.org_id == org_id)
    if types:
        stmt = stmt.where(NotificationRow.type.in_(types))
    rows = list((await session.execute(stmt)).scalars().all())
    return [Notification.from_row(r) for r in rows]


async def popover_for_user(
    session: AsyncSession, *, user_id: UUID, limit: int = 10
) -> tuple[list[Notification], int]:
    """Latest N unread for the sidebar bell + the unread count."""
    rows_q = (
        select(NotificationRow)
        .where(NotificationRow.user_id == user_id, NotificationRow.read_at.is_(None))
        .order_by(desc(NotificationRow.created_at))
        .limit(limit)
    )
    count_q = select(NotificationRow.id).where(
        NotificationRow.user_id == user_id, NotificationRow.read_at.is_(None)
    )
    rows = list((await session.execute(rows_q)).scalars().all())
    unread_count = len((await session.execute(count_q)).scalars().all())
    return [Notification.from_row(r) for r in rows], unread_count


async def mark_read(session: AsyncSession, *, user_id: UUID, notification_id: UUID) -> Notification | None:
    row = (
        await session.execute(
            select(NotificationRow).where(
                NotificationRow.id == notification_id, NotificationRow.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await session.flush()
    return Notification.from_row(row)


async def mark_all_read(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID | None = None,
    types: list[str] | None = None,
) -> int:
    """Returns the number of rows marked. Idempotent."""
    stmt = (
        update(NotificationRow)
        .where(NotificationRow.user_id == user_id, NotificationRow.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    if org_id is not None:
        stmt = stmt.where(NotificationRow.org_id == org_id)
    if types:
        stmt = stmt.where(NotificationRow.type.in_(types))
    result = await session.execute(stmt)
    return int(result.rowcount or 0)
