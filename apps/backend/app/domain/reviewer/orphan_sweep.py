"""Periodic safeguard against `running` tickets that the reviewer never picked up.

A ticket lands in `running` the moment the github intake type inserts it.
The reviewer then schedules a `reviews` row and drives the workflow forward.
When that hand-off silently fails (missing BYOK key, transient crash mid-
dispatch, etc.) the ticket is stranded — no `reviews` row ever appears, so the
workflow projection never moves status off `running`, and the Dashboard's
"in flight" band counts it forever.

This sweep flips stranded rows to `failed` with `failure_reason=
'orphaned_no_review_job'`. POC scope: small interval loop, no retries, no
state-machine refinement. The grace window (`yaaos_ticket_orphan_grace_seconds`)
keeps freshly-inserted rows safe while their reviewer dispatch is in-flight.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import and_, not_, select
from sqlalchemy.sql import exists

from app.core.config import get_settings
from app.core.database import session as db_session
from app.domain import tickets
from app.domain.reviewer.models import ReviewRow
from app.domain.tickets import TicketRow

log = structlog.get_logger("reviewer.orphan_sweep")

ORPHAN_REASON = "orphaned_no_review_job"


async def _sweep_once() -> int:
    """One pass over the orphan candidates. Returns the number of tickets failed."""
    grace = get_settings().yaaos_ticket_orphan_grace_seconds
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(seconds=grace)

    async with db_session() as s:
        stmt = select(TicketRow.id, TicketRow.org_id).where(
            and_(
                TicketRow.status == "running",
                TicketRow.created_at < cutoff,
                not_(
                    exists().where(
                        and_(
                            ReviewRow.pr_id == TicketRow.pr_id,
                            TicketRow.pr_id.is_not(None),
                        )
                    )
                ),
            )
        )
        rows = (await s.execute(stmt)).all()

    failed = 0
    for ticket_id, org_id in rows:
        try:
            await tickets.fail(ticket_id, reason=ORPHAN_REASON, org_id=org_id)
            failed += 1
        except Exception:
            # `_transition` rejects terminal states (won), or another sweep
            # process beat us. Either way, not fatal — log and move on.
            log.warning("orphan_sweep.transition_failed", ticket_id=str(ticket_id))
    if failed:
        log.info("orphan_sweep.swept", count=failed)
    return failed


async def run_sweep_loop() -> None:
    """Forever-loop: every `yaaos_ticket_orphan_sweep_interval_seconds`, sweep."""
    interval = get_settings().yaaos_ticket_orphan_sweep_interval_seconds
    while True:
        try:
            await _sweep_once()
        except Exception:
            log.exception("orphan_sweep.failed")
        await asyncio.sleep(interval)
