"""Durable task handlers for `domain/notifications`.

`handle_ticket_status_change` — receives a pre-computed list of recipient
user IDs from the producer and writes one notification row per user via
`service.record`. The `org_id` contextvar is set automatically by the
`OrgContextMiddleware` in `core/tasks` — the body never calls
`org_context` itself.

Producers compute `member_user_ids` inside their own transaction, atomic
with the status change, so this body does no membership query.
"""

from __future__ import annotations

from uuid import UUID

from app.core.database import session as db_session
from app.core.tasks import TaskRef, task
from app.domain.notifications import service as notif_service
from app.domain.tickets import get_by_id as get_ticket_by_id

_NEW_STATUS_TO_NOTIF_TYPE: dict[str, str] = {
    "hitl": "hitl_waiting",
    "done": "ticket_completed",
    "failed": "ticket_failed",
}

_TITLE_TEMPLATE: dict[str, str] = {
    "hitl_waiting": "Reviewer needs your input",
    "ticket_completed": "Review complete",
    "ticket_failed": "Review failed",
}


async def _handle_ticket_status_change(
    *,
    ticket_id: UUID,
    member_user_ids: list[UUID],
    org_id: UUID,
    new_status: str,
) -> None:
    notif_type = _NEW_STATUS_TO_NOTIF_TYPE.get(new_status)
    if notif_type is None:
        return

    ticket = await get_ticket_by_id(ticket_id)
    if ticket is None:
        return

    title = _TITLE_TEMPLATE[notif_type]
    body = ticket.title

    async with db_session() as s:
        for user_id in member_user_ids:
            await notif_service.record(
                user_id=user_id,
                org_id=org_id,
                type=notif_type,
                title=title,
                body=body,
                ticket_id=ticket_id,
                session=s,
            )
        await s.commit()


handle_ticket_status_change: TaskRef = task(
    name="notifications.handle_ticket_status_change",
)(_handle_ticket_status_change)
