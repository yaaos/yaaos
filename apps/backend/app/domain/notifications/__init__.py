"""domain/notifications — cross-org user inbox."""

from app.domain.notifications import web  # noqa: F401
from app.domain.notifications.service import (
    Notification,
    list_for_user,
    mark_all_read,
    mark_read,
    popover_for_user,
    record,
)

__all__ = [
    "Notification",
    "list_for_user",
    "mark_all_read",
    "mark_read",
    "popover_for_user",
    "record",
]
