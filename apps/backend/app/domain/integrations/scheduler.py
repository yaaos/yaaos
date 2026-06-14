"""Hourly health-check loop for `mcp_credentials`.

Walks every enabled credential, calls the provider's `validate(access_token)`,
flips `last_refresh_status` to `"ok"` or `"failed"`, and enqueues an email
notification to the org's Owners on transition-to-failed (deduplicated to
once per 24h via `last_failure_notified_at`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel
from sqlalchemy import select

from app.core.audit_log import Actor, ActorKind, audit
from app.core.auth import Role, org_context
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.identity import repository as identity_repo
from app.core.secrets import SecretsDecryptError, decrypt
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.types import get_provider
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import send_plain

log = structlog.get_logger("integrations.scheduler")


_FAILURE_NOTIFICATION_DEDUP = timedelta(hours=24)


class _HealthCheckFailedPayload(BaseModel):
    provider: str


def _broken_creds_email_subject(provider: str) -> str:
    return f"[yaaos] {provider} integration disconnected — action required"


def _broken_creds_email_body(*, provider: str, org_slug: str) -> str:
    base = get_settings().yaaos_app_base_url
    return (
        f"Your {provider} integration in yaaos has stopped working — the most recent "
        "health check failed.\n\n"
        f"Reconnect at {base}/org/{org_slug}/settings/integrations.\n\n"
        "Reviews running while a provider is in this state will still proceed, but "
        f"the agent will receive `broken_creds` errors when it tries to call {provider} "
        "tools.\n"
    )


async def _notify_owners(row: McpCredentialRow) -> int:
    """Send the broken-creds email to every Owner of the org. Returns count sent."""
    async with db_session() as s:
        org = await orgs_repo.get_org(s, row.org_id)
        if org is None:
            return 0
        memberships = await orgs_repo.list_memberships_for_org(s, row.org_id)
        owner_ids = [m.user_id for m in memberships if m.role == Role.OWNER]
        if not owner_ids:
            return 0
        # Collect verified email addresses for each owner.
        owner_emails: list[str] = []
        for uid in owner_ids:
            for email_row in await identity_repo.list_emails_for_user(s, uid):
                if email_row.verified_at is not None:
                    owner_emails.append(email_row.email)
    if not owner_emails:
        return 0
    subject = _broken_creds_email_subject(row.provider)
    body = _broken_creds_email_body(provider=row.provider, org_slug=org.slug)
    sent = 0
    for to in owner_emails:
        try:
            await send_plain(to=to, subject=subject, body=body)
            sent += 1
        except Exception as exc:
            # inside-span failure: spawned inside spawn:integrations.scheduler span
            _span = trace.get_current_span()
            _span.record_exception(exc)
            _span.set_status(StatusCode.ERROR, str(exc))
            log.exception("integrations.notify_owners.send_failed", to=to, provider=row.provider)
    return sent


async def run_health_check_once() -> dict[str, int]:
    """One pass over enabled credentials. Returns a counts summary."""
    now = datetime.now(UTC)
    counts = {"checked": 0, "ok": 0, "failed": 0, "notified": 0}
    async with db_session() as s:
        rows = list(
            (await s.execute(select(McpCredentialRow).where(McpCredentialRow.enabled.is_(True))))
            .scalars()
            .all()
        )
    for row in rows:
        # Per-row org_context so structlog + OTel + audit emissions carry the
        # right org_id + actor_kind=system tags for this iteration.
        async with org_context(row.org_id, ActorKind.SYSTEM):
            counts["checked"] += 1
            prov = get_provider(row.provider)
            if prov is None:
                continue
            try:
                access = decrypt(row.encrypted_access_token.encode()).decode()
            except SecretsDecryptError as exc:
                # inside-span failure: spawned inside spawn:integrations.scheduler span
                _span = trace.get_current_span()
                _span.record_exception(exc)
                _span.set_status(StatusCode.ERROR, str(exc))
                log.exception("integrations.health_check.decrypt_failed", provider=row.provider)
                ok = False
            else:
                try:
                    ok = await prov.validate(access)
                except Exception as exc:
                    # inside-span failure: spawned inside spawn:integrations.scheduler span
                    _span = trace.get_current_span()
                    _span.record_exception(exc)
                    _span.set_status(StatusCode.ERROR, str(exc))
                    log.exception("integrations.health_check.validate_crashed", provider=row.provider)
                    ok = False
            async with db_session() as s:
                refreshed = (
                    await s.execute(
                        select(McpCredentialRow).where(
                            McpCredentialRow.org_id == row.org_id,
                            McpCredentialRow.provider == row.provider,
                        )
                    )
                ).scalar_one_or_none()
                if refreshed is None:
                    continue
                if ok:
                    refreshed.last_validated_at = now
                    refreshed.last_refresh_status = "ok"
                    refreshed.last_refresh_failed_at = None
                    counts["ok"] += 1
                    await s.commit()
                    continue
                refreshed.last_refresh_status = "failed"
                refreshed.last_refresh_failed_at = now
                await audit(
                    "org",
                    refreshed.org_id,
                    f"mcp.{refreshed.provider}.token_refresh_failed",
                    _HealthCheckFailedPayload(provider=refreshed.provider),
                    Actor.system(),
                    org_id=refreshed.org_id,
                    session=s,
                )
                counts["failed"] += 1
                # Dedup: only notify if first failure (null) or stale (>24h).
                should_notify = (
                    refreshed.last_failure_notified_at is None
                    or refreshed.last_failure_notified_at < now - _FAILURE_NOTIFICATION_DEDUP
                )
                if should_notify:
                    refreshed.last_failure_notified_at = now
                await s.commit()
            if should_notify:
                sent = await _notify_owners(refreshed)
                if sent:
                    counts["notified"] += sent
    return counts


async def run_scheduler_loop() -> None:
    """Forever-loop: health-check every `yaaos_integrations_health_check_interval_seconds`.
    Tests get this with the interval set to 1 second; production uses 3600."""
    interval = get_settings().yaaos_integrations_health_check_interval_seconds
    while True:
        try:
            counts = await run_health_check_once()
            if any(counts.values()):
                log.debug("integrations.health_check.ran", **counts)
        except Exception as exc:
            # inside-span failure: spawned inside spawn:integrations.scheduler span
            _span = trace.get_current_span()
            _span.record_exception(exc)
            _span.set_status(StatusCode.ERROR, str(exc))
            log.exception("integrations.scheduler.failed")
        await asyncio.sleep(interval)
