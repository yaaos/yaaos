"""Status-change notification policy for `domain/tickets`.

`build_status_change_specs` is the single owner of which ticket statuses
generate notifications, which type to assign, and what title to show.
Callers enqueue `core.notifications.fanout` with the returned specs; this
module has no task body of its own.
"""

from __future__ import annotations

from uuid import UUID

from app.core.notifications import NotificationSpec

# Only terminal / attention-requiring transitions produce notifications.
_STATUS_TO_NOTIF_TYPE: dict[str, str] = {
    "hitl": "hitl_waiting",
    "done": "ticket_completed",
    "failed": "ticket_failed",
}

_TITLE_TEMPLATE: dict[str, str] = {
    "hitl_waiting": "Reviewer needs your input",
    "ticket_completed": "Review complete",
    "ticket_failed": "Review failed",
}


def build_status_change_specs(
    *,
    ticket_id: UUID,
    org_id: UUID,
    ticket_title: str,
    member_user_ids: list[UUID],
    new_status: str,
) -> list[NotificationSpec]:
    """Return one `NotificationSpec` per member for a ticket status change.

    Returns an empty list when `new_status` does not warrant notifications
    (e.g. `running`, `cancelled`). Callers pass the title from their
    in-transaction snapshot; no re-fetch required.
    """
    notif_type = _STATUS_TO_NOTIF_TYPE.get(new_status)
    if notif_type is None:
        return []

    title = _TITLE_TEMPLATE[notif_type]
    return [
        NotificationSpec(
            user_id=user_id,
            org_id=org_id,
            type=notif_type,
            title=title,
            body=ticket_title,
            subject_type="ticket",
            subject_id=ticket_id,
        )
        for user_id in member_user_ids
    ]
