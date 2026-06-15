"""Hourly sweep of expired, unaccepted invitations — `@scheduled` worker task.

Runs as a `@scheduled` worker task (cron `0 * * * *`) — exactly one worker
pod enqueues each hourly slot via the `ON CONFLICT DO NOTHING` claim.
"""

from __future__ import annotations

import structlog

from app.core.tasks import scheduled
from app.domain.orgs.service import delete_expired_invitations

log = structlog.get_logger("orgs.invitation_sweep")


async def _sweep_once() -> None:
    """One pass: purge expired invitations."""
    purged = await delete_expired_invitations()
    if purged:
        log.debug("orgs.invitations.swept", purged=purged)


# Hourly sweep — cluster-safe via `core/tasks` per-tick claim.
# Exactly one worker pod enqueues per slot. Body is idempotent.
invitation_sweep = scheduled(
    name="invitation_sweep",
    cron="0 * * * *",
    queue="default",
    max_retries=1,
)(_sweep_once)
