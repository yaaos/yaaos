"""Background subscriber that turns workflow events into notification rows.

Spawned at app startup from `web.py`'s `on_startup` hook. Subscribes to
the in-process event bus, filters for the three transitions we care about
(ticket_status_changed → hitl / done / failed), looks up the ticket's
org members, and writes one notification per member via
`service.record(...)`.

POC notification model (per api-changes.md): every active member of the
ticket's org gets the row, idempotent on `(user_id, type, ticket_id)`.
A future refinement narrows it to the user who triggered the ticket +
the org's admins; the SPA already filters by `read_state` and `org_id`
on the inbox page, so over-inclusive writes are harmless.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from app.core.database import session as db_session
from app.core.events import EventFilter, subscribe
from app.core.observability import spawn
from app.domain.notifications import service as notif_service
from app.domain.tickets import get_by_id as get_ticket_by_id

if TYPE_CHECKING:
    from app.domain.tickets import TicketStatusChanged

log = structlog.get_logger("notifications.subscribers")


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

_run_task: asyncio.Task[None] | None = None


async def _run() -> None:
    """Long-running consumer: pull every ticket_status_changed event off the
    bus and dispatch to `_handle_status_change`."""
    filt = EventFilter(kinds=["ticket_status_changed"])
    async for event in subscribe(filt):
        # The filter constrains kind; downcast via duck typing — the
        # bus erases the concrete type.
        new_status = getattr(event, "new_status", None)
        if not isinstance(new_status, str):
            continue
        try:
            await _handle_status_change(event)
        except Exception:
            # Single subscriber must survive bad events — log and keep
            # consuming.
            log.exception(
                "notifications.subscriber.handler_failed",
                kind=event.kind,
                ticket_id=str(event.ticket_id),
            )


async def _handle_status_change(event: TicketStatusChanged) -> None:
    notif_type = _NEW_STATUS_TO_NOTIF_TYPE.get(event.new_status)
    if notif_type is None or event.ticket_id is None:
        return

    ticket = await get_ticket_by_id(event.ticket_id)
    if ticket is None:
        return

    async with db_session() as s:
        members = await _list_active_member_ids(s, org_id=ticket.org_id)
        title = _TITLE_TEMPLATE[notif_type]
        body = ticket.title
        for user_id in members:
            await notif_service.record(
                user_id=user_id,
                org_id=ticket.org_id,
                type=notif_type,
                title=title,
                body=body,
                ticket_id=ticket.id,
                session=s,
            )
        await s.commit()


async def _list_active_member_ids(session, *, org_id: UUID) -> list[UUID]:  # type: ignore[no-untyped-def]
    """Returns user_ids for every membership row of the org.

    `domain/notifications` is a leaf module — it can't import
    `domain/orgs` without creating a back-edge. We read the table
    directly via a raw SELECT through the shared session.
    """
    from sqlalchemy import text  # noqa: PLC0415

    rows = (
        await session.execute(
            text("SELECT user_id FROM memberships WHERE org_id = :org_id"),
            {"org_id": org_id},
        )
    ).all()
    return [r[0] for r in rows]


async def start_subscriber() -> None:
    """Spawn the background subscriber. Idempotent."""
    global _run_task
    if _run_task is not None and not _run_task.done():
        return
    _run_task = spawn("notifications.subscriber", _run())
