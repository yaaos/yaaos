"""Periodic sweep of expired, unaccepted invitations.

Spawned in the FastAPI lifespan via `orgs.web`'s `RouteSpec.on_startup`
hook. Runs on the same cadence as the identity cleanup loop.
"""

from __future__ import annotations

import asyncio

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.core.config import get_settings
from app.domain.orgs.service import delete_expired_invitations

log = structlog.get_logger("orgs.invitation_sweep")


async def run_invitation_sweep_loop() -> None:
    """Forever-loop: every `yaaos_auth_cleanup_interval_seconds`, purge expired invitations."""
    interval = get_settings().yaaos_auth_cleanup_interval_seconds
    while True:
        try:
            purged = await delete_expired_invitations()
            if purged:
                log.debug("orgs.invitations.swept", purged=purged)
        except Exception as exc:
            # inside-span failure: spawned inside spawn:orgs.invitation_sweep span
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            log.exception("orgs.invitations.sweep.failed")
        await asyncio.sleep(interval)
