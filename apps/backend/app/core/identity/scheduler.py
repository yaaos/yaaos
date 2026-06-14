"""Hourly purge of expired sessions, unverified-TOTP secrets older than
24h, and audit-log entries older than `AUDIT_LOG_RETENTION`.

Invitation expiry is swept by `domain/orgs`'s own scheduled task.

Single `@scheduled` body registered with `core/tasks`; runs every hour
on the minute (`0 * * * *`). Cluster-safe (per-tick atomic claim in
`core/tasks`) — at most one worker enqueues the body each hour. The
body itself is idempotent: every purge step is a bare `DELETE` filtered
by a wall-clock cutoff, so a redelivery from the outbox-drain retry
path is a no-op.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from sqlalchemy import delete as sql_delete

from app.core.audit_log import AUDIT_LOG_RETENTION
from app.core.audit_log import purge_older_than as purge_audit_older_than
from app.core.database import session as db_session
from app.core.identity import sessions
from app.core.identity.models import UserTotpSecretRow
from app.core.tasks import scheduled

log = structlog.get_logger("identity.cleanup")

UNVERIFIED_TOTP_TTL = timedelta(hours=24)


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
    from app.core.tenancy import list_memberships_for_user as _list_memberships  # noqa: PLC0415

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
            for m in await _list_memberships(s, user_id):
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


async def run_identity_purge() -> None:
    """Body of the hourly `identity_purge` `@scheduled` task. Runs each
    of the three purge passes; logs combined counts when any row was
    affected. Idempotent — re-running is a no-op.

    Module-public so service tests can invoke the body directly without
    going through the broker dispatch path.
    """
    try:
        sessions_purged = await _purge_expired_sessions()
        totps_purged = await _purge_stale_unverified_totp_secrets()
        audit_purged = await _purge_old_audit_entries()
    except Exception as exc:
        # inside-span failure: taskiq wraps scheduled task bodies in a span
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, str(exc))
        log.exception("identity.cleanup.failed")
        raise
    if sessions_purged or totps_purged or audit_purged:
        log.debug(
            "identity.cleanup.ran",
            sessions_purged=sessions_purged,
            totps_purged=totps_purged,
            audit_purged=audit_purged,
        )


# Hourly at minute 0 — the cadence the spec mandates.
identity_purge = scheduled(
    name="identity_purge",
    cron="0 * * * *",
    queue="default",
    max_retries=1,
)(run_identity_purge)
