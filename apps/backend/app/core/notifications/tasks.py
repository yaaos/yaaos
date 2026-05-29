"""Durable task handlers for `core/notifications`.

`fanout` — receives a list of `NotificationSpec` dicts and writes one
notification row per spec via `service.create`. The `org_id` contextvar is
set automatically by the `OrgContextMiddleware` in `core/tasks` — the body
never calls `org_context` itself.

Producers pre-compute recipients inside their own transaction, atomic with
the event that triggered the notifications, so this body does no membership
or entity query.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.database import session as db_session
from app.core.notifications import service as notif_service
from app.core.tasks import TaskRef, task


@dataclass
class NotificationSpec:
    """Value object describing a single notification to write."""

    user_id: UUID
    org_id: UUID
    type: str
    title: str
    body: str
    subject_type: str | None = None
    subject_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": str(self.user_id),
            "org_id": str(self.org_id),
            "type": self.type,
            "title": self.title,
            "body": self.body,
            "subject_type": self.subject_type,
            "subject_id": str(self.subject_id) if self.subject_id is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NotificationSpec:
        return cls(
            user_id=UUID(d["user_id"]),
            org_id=UUID(d["org_id"]),
            type=d["type"],
            title=d["title"],
            body=d["body"],
            subject_type=d.get("subject_type"),
            subject_id=UUID(d["subject_id"]) if d.get("subject_id") else None,
        )


async def _fanout(*, specs: list[dict[str, Any]]) -> None:
    parsed = [NotificationSpec.from_dict(s) for s in specs]
    async with db_session() as s:
        for spec in parsed:
            await notif_service.create(
                user_id=spec.user_id,
                org_id=spec.org_id,
                type=spec.type,
                title=spec.title,
                body=spec.body,
                subject_type=spec.subject_type,
                subject_id=spec.subject_id,
                session=s,
            )
        await s.commit()


fanout: TaskRef = task(
    name="notifications.fanout",
)(_fanout)
