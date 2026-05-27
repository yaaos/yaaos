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
from uuid import UUID

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import session as db_session
from app.domain import tickets
from app.domain.reviewer.models import ReviewRow

log = structlog.get_logger("reviewer.orphan_sweep")

ORPHAN_REASON = "orphaned_no_review_job"


async def _sweep_once() -> int:
    """One pass over the orphan candidates. Returns the number of tickets failed."""
    grace = get_settings().yaaos_ticket_orphan_grace_seconds
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(seconds=grace)

    # Fetch running tickets older than the grace window via the public tickets
    # surface. Each triple is (ticket_id, org_id, pr_id).
    candidates = await tickets.list_running_older_than(cutoff)
    if not candidates:
        return 0

    # Partition: tickets with a pr_id need a ReviewRow check (intra-reviewer
    # model — no module boundary violation). Tickets without a pr_id are
    # always orphan candidates.
    pr_ids_to_ticket: dict[UUID, tuple[UUID, UUID]] = {}
    no_pr_candidates: list[tuple[UUID, UUID]] = []
    for ticket_id, org_id, pr_id in candidates:
        if pr_id is not None:
            pr_ids_to_ticket[pr_id] = (ticket_id, org_id)
        else:
            no_pr_candidates.append((ticket_id, org_id))

    # Find which pr_ids already have a ReviewRow (those are not orphans).
    covered_pr_ids: set[UUID] = set()
    if pr_ids_to_ticket:
        async with db_session() as s:
            covered_pr_ids = {
                r[0]
                for r in (
                    await s.execute(
                        select(ReviewRow.pr_id).where(ReviewRow.pr_id.in_(list(pr_ids_to_ticket)))
                    )
                ).all()
            }

    orphans: list[tuple[UUID, UUID]] = list(no_pr_candidates)
    for pr_id, (ticket_id, org_id) in pr_ids_to_ticket.items():
        if pr_id not in covered_pr_ids:
            orphans.append((ticket_id, org_id))

    failed = 0
    for ticket_id, org_id in orphans:
        try:
            await tickets.fail(ticket_id, reason=ORPHAN_REASON, org_id=org_id)
            failed += 1
        except Exception:
            # `_transition` rejects terminal states, or another sweep process
            # beat us. Either way, not fatal — log and move on.
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
