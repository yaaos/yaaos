"""Periodic cleanup of expired sessions, expired invitations, and
unverified-TOTP secrets older than 24h.

Single background loop owned by `domain/identity`. Spawned in the FastAPI
lifespan via the module's `RouteSpec.on_startup` hook (see `web.py`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete as sql_delete

from app.core.config import get_settings
from app.core.database import session as db_session
from app.domain.identity import sessions
from app.domain.identity.models import UserTotpSecretRow
from app.domain.orgs.models import InvitationRow

log = structlog.get_logger("identity.cleanup")

UNVERIFIED_TOTP_TTL = timedelta(hours=24)


async def _purge_expired_invitations() -> int:
    async with db_session() as s:
        result = await s.execute(
            sql_delete(InvitationRow)
            .where(
                InvitationRow.expires_at < datetime.now(UTC),
                InvitationRow.accepted_at.is_(None),
            )
            .returning(InvitationRow.id)
        )
        n = len(result.all())
        await s.commit()
        return n


async def _purge_stale_unverified_totp_secrets() -> int:
    cutoff = datetime.now(UTC) - UNVERIFIED_TOTP_TTL
    async with db_session() as s:
        result = await s.execute(
            sql_delete(UserTotpSecretRow)
            .where(
                UserTotpSecretRow.verified_at.is_(None),
                UserTotpSecretRow.created_at < cutoff,
            )
            .returning(UserTotpSecretRow.user_id)
        )
        n = len(result.all())
        await s.commit()
        return n


async def _purge_expired_sessions() -> int:
    async with db_session() as s:
        n = await sessions.cleanup_expired(s)
        await s.commit()
        return n


async def run_cleanup_loop() -> None:
    """Forever-loop: every `yaaos_auth_cleanup_interval_seconds`, purge.

    Tests get this with the interval set to 1 second; production uses 3600.
    """
    interval = get_settings().yaaos_auth_cleanup_interval_seconds
    while True:
        try:
            sessions_purged = await _purge_expired_sessions()
            invitations_purged = await _purge_expired_invitations()
            totps_purged = await _purge_stale_unverified_totp_secrets()
            if sessions_purged or invitations_purged or totps_purged:
                log.info(
                    "identity.cleanup.ran",
                    sessions_purged=sessions_purged,
                    invitations_purged=invitations_purged,
                    totps_purged=totps_purged,
                )
        except Exception:
            log.exception("identity.cleanup.failed")
        await asyncio.sleep(interval)
