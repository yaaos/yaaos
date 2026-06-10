"""Periodic safeguard against `running` tickets whose workflow never started.

A ticket lands in `running` the moment the github intake type inserts it.
The engine then starts a `workflow_executions` row and drives the steps forward.
When that hand-off silently fails (missing BYOK key, transient crash mid-
dispatch, etc.) the ticket is stranded — no `workflow_executions` row ever
leaves the non-terminal states, so the workflow projection never moves status
off `running`, and the Dashboard's "in flight" band counts it forever.

This sweep flips stranded rows to `failed` with `failure_reason=
'orphaned_no_review_job'`. The orphan signal is: no non-terminal
`workflow_executions` row exists for the ticket. A ticket with an active
execution (e.g. stalled at `ProvisionWorkspace`) is explicitly NOT an orphan —
the agent may still come online and complete the review. The grace window
(`yaaos_ticket_orphan_grace_seconds`) keeps freshly-inserted rows safe while
their workflow dispatch is in-flight.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import structlog

from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.workflow import list_active_execution_ids
from app.domain import tickets

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

    # A candidate is an orphan iff it has no non-terminal workflow execution.
    # A ticket stalled at an early step (e.g. ProvisionWorkspace) has a running
    # execution and must not be swept — the agent may still complete it.
    orphans: list[tuple[UUID, UUID]] = []
    async with db_session() as s:
        for ticket_id, org_id, _ in candidates:
            active = await list_active_execution_ids(ticket_id, session=s)
            if not active:
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
