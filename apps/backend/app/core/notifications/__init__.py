"""core/notifications — generic cross-org user inbox."""

from app.core.notifications import web  # noqa: F401
from app.core.notifications.service import (
    Notification,
    create,
    list_for_user,
    mark_all_read,
    mark_read,
    popover_for_user,
)
from app.core.notifications.tasks import NotificationSpec, fanout

__all__ = [
    "Notification",
    "NotificationSpec",
    "create",
    "fanout",
    "list_for_user",
    "mark_all_read",
    "mark_read",
    "popover_for_user",
]
