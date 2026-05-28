"""Periodic cleanup of expired sessions, expired invitations,
unverified-TOTP secrets older than 24h, and audit-log entries older
than `AUDIT_LOG_RETENTION`.

Single background loop owned by `core/identity`. Spawned in the FastAPI
lifespan via the module's `RouteSpec.on_startup` hook (see `web.py`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete as sql_delete

from app.core.audit_log import AUDIT_LOG_RETENTION
from app.core.audit_log import purge_older_than as purge_audit_older_than
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.identity import sessions
from app.core.identity.models import UserTotpSecretRow
from app.domain.orgs import delete_expired_invitations

log = structlog.get_logger("identity.cleanup")

UNVERIFIED_TOTP_TTL = timedelta(hours=24)


async def _purge_expired_invitations() -> int:
    return await delete_expired_invitations()


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
    """Purge expired session rows. Each purged session emits a `logout`
    audit row with `kind=expiry` so the audit timeline reflects every
    logout-style event (explicit, forced, expiry) per the spec."""
    from pydantic import BaseModel as _BaseModel  # noqa: PLC0415
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.core.audit_log import Actor as _Actor  # noqa: PLC0415
    from app.core.audit_log import audit as _audit  # noqa: PLC0415
    from app.core.identity.models import SessionRow  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    class _ExpiryPayload(_BaseModel):
        kind: str = "expiry"

    async with db_session() as s:
        # Collect about-to-be-purged sessions so we can emit audit rows per
        # affected (user, org) pair before deletion.
        expired = (
            await s.execute(
                _select(SessionRow.token_hash, SessionRow.user_id).where(
                    SessionRow.expires_at < datetime.now(UTC), SessionRow.user_id.is_not(None)
                )
            )
        ).all()
        for _token_hash, user_id in expired:
            if user_id is None:
                continue
            for m in await orgs_repo.list_memberships_for_user(s, user_id):
                await _audit(
                    "user",
                    user_id,
                    "logout",
                    _ExpiryPayload(),
                    _Actor(kind="system"),
                    org_id=m.org_id,
                    session=s,
                )
        n = await sessions.cleanup_expired(s)
        await s.commit()
        return n


async def _purge_old_audit_entries() -> int:
    return await purge_audit_older_than(datetime.now(UTC) - AUDIT_LOG_RETENTION)


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
            audit_purged = await _purge_old_audit_entries()
            if sessions_purged or invitations_purged or totps_purged or audit_purged:
                log.info(
                    "identity.cleanup.ran",
                    sessions_purged=sessions_purged,
                    invitations_purged=invitations_purged,
                    totps_purged=totps_purged,
                    audit_purged=audit_purged,
                )
        except Exception:
            log.exception("identity.cleanup.failed")
        await asyncio.sleep(interval)
