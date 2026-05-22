"""Per-PR queue discipline + the review-job runner.

One review job per (PR x review run). The runner provisions a workspace, calls
the coding agent (which dispatches yaaos-* subagents internally and synthesizes
their findings), and posts one Review to the VCS.

Reply / verify-fix flows are deferred — a future `review_comments` table will
own that lifecycle separately. For now no reply path exists.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func as sa_func
from sqlalchemy import select, update

from app.core.audit_log import Actor, audit_for_review_job
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.events import publish
from app.core.observability import spawn
from app.domain import (
    pull_requests,
    tickets,
)
from app.domain.coding_agent import FindingDraft
from app.domain.reviewer.aggregate import RawFinding
from app.domain.reviewer.constants import (
    DEFAULT_EFFORT as _DEFAULT_EFFORT,
)
from app.domain.reviewer.constants import (
    DEFAULT_MODEL as _DEFAULT_MODEL,
)
from app.domain.reviewer.constants import (
    M01_ORG_ID,
)
from app.domain.reviewer.legacy_runner import (
    _run_review_job_with_context,
    _utcnow,
)
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.models import ReviewRow
from app.domain.reviewer.queue_events import (
    ReviewJobStatusChanged,
)
from app.domain.reviewer.review_job import ReviewJobInput
from app.domain.reviewer.review_job_transitions import (
    CancelledPayload as _CancelledPayload,
)
from app.domain.reviewer.review_job_transitions import (
    FailedPayload as _FailedPayload,
)
from app.domain.reviewer.review_job_transitions import (
    ScheduledPayload as _ScheduledPayload,
)

log = structlog.get_logger("reviewer")

# In-flight task registry, keyed by review_job_id. Used by `cancel_pending`
# to interrupt the running coro mid-CLI: flipping the DB row to cancelled
# alone leaves the subprocess running until its own timeout. Cancelling the
# asyncio task propagates `CancelledError` down through `coding_agent.review`
# → `workspace.run_coding_agent_cli`, which kills the subprocess group
# (SIGTERM → 2s → SIGKILL) before the cancellation unwinds further. The
# done callback removes entries automatically; this registry is best-effort
# (only useful for tasks spawned in the current process — see `startup_recovery`
# for the cross-restart story).
_inflight_tasks: dict[UUID, asyncio.Task[None]] = {}


def _register_inflight(job_id: UUID, task: asyncio.Task[None]) -> None:
    _inflight_tasks[job_id] = task
    task.add_done_callback(lambda _t: _inflight_tasks.pop(job_id, None))


# Audit payloads moved to `domain/reviewer/review_job_transitions.py`
# (slice 45). Rebound under the legacy underscore-prefixed names at the
# top of the file.


# ── Public API ────────────────────────────────────────────────────────────────


# `ReviewJob` + `ReviewJobInput` moved to `domain/reviewer/review_job.py`
# (slice 43). Re-imported at the top of the file under the same names.


async def schedule_review(
    ticket_id: UUID,
    *,
    trigger_reason: str,
    actor: Actor,
    org_id: UUID,
) -> UUID | None:
    """Schedule one review run for the ticket's PR.

    Cancels any in-flight job for the same PR (superseded). Returns the new
    job id, or None if the ticket has no PR.
    """
    ticket = await tickets.get(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        return None
    pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
    debounce = get_settings().yaaos_review_debounce_seconds

    new_id = uuid4()
    # Acquire the per-PR advisory lock for the whole cancel + insert flow so
    # the sequence_number computation can't race two concurrent schedule_review
    # calls into the UNIQUE(pr_id, sequence_number) constraint.
    async with db_session() as s:
        await acquire_pr_lock(s, pr.id)
        # Cancel any in-flight review for this PR.
        inflight = (
            (
                await s.execute(
                    select(ReviewRow).where(
                        ReviewRow.pr_id == pr.id,
                        ReviewRow.status.in_(["queued", "running"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inflight:
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == row.id)
                .values(
                    status="cancelled",
                    skip_reason="superseded",
                    completed_at=_utcnow(),
                    # Plan §6.3 / §7: the cancelled row points at the new
                    # review that superseded it so the UI / audit can chain
                    # them.
                    superseded_by_review_id=new_id,
                )
            )
        max_seq = (
            await s.execute(
                select(sa_func.coalesce(sa_func.max(ReviewRow.sequence_number), 0)).where(
                    ReviewRow.pr_id == pr.id
                )
            )
        ).scalar_one()
        s.add(
            ReviewRow(
                id=new_id,
                org_id=org_id,
                pr_id=pr.id,
                sequence_number=max_seq + 1,
                status="queued",
                trigger_reason=trigger_reason,
                destination="vcs",
                # Generation-2 scope: schedule_review is always a "full" run.
                # Incremental scope is owned by handle_push (§6.2).
                scope_kind="full",
                model=_DEFAULT_MODEL,
                effort=_DEFAULT_EFFORT,
            )
        )
        for row in inflight:
            await audit_for_review_job(
                row.id,
                "review_job.cancelled",
                _CancelledPayload(reason="superseded"),
                actor=actor,
                org_id=org_id,
                session=s,
            )
        await audit_for_review_job(
            new_id,
            "review_job.scheduled",
            _ScheduledPayload(trigger_reason=trigger_reason, debounce_seconds=debounce),
            actor=actor,
            org_id=org_id,
            session=s,
        )
        await s.commit()
    await publish(ReviewJobStatusChanged(pr_id=pr.id, review_job_id=new_id, status="queued"))
    task = spawn(
        f"review_job:{new_id}",
        _run_review_job_with_context(
            ReviewJobInput(
                review_job_id=new_id,
                ticket_id=ticket_id,
                org_id=org_id,
                debounce_seconds=debounce,
            )
        ),
    )
    _register_inflight(new_id, task)
    return new_id


async def cancel_pending(
    ticket_id: UUID, *, actor: Actor, org_id: UUID, reason: str = "ticket_closed"
) -> int:
    ticket = await tickets.get(ticket_id, org_id=org_id)
    if ticket.pr_id is None:
        return 0
    async with db_session() as s:
        inflight = (
            (
                await s.execute(
                    select(ReviewRow).where(
                        ReviewRow.pr_id == ticket.pr_id,
                        ReviewRow.status.in_(["queued", "running"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inflight:
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.id == row.id)
                .values(status="cancelled", skip_reason=reason, completed_at=_utcnow())
            )
            await audit_for_review_job(
                row.id,
                "review_job.cancelled",
                _CancelledPayload(reason=reason),
                actor=actor,
                org_id=org_id,
                session=s,
            )
        await s.commit()
    for row in inflight:
        # Interrupt the in-flight coro so the CLI subprocess is actually
        # killed — not just left running until its own timeout. The asyncio
        # cancel propagates through `coding_agent.review` →
        # `workspace.run_coding_agent_cli`, which catches `CancelledError`
        # and kills the subprocess group before the cancellation unwinds.
        # If no task is registered (e.g., post-restart), this is a no-op;
        # the DB row is already in `cancelled` and that's what the UI shows.
        task = _inflight_tasks.get(row.id)
        if task is not None and not task.done():
            task.cancel()
    return len(inflight)


# ── Read API ──────────────────────────────────────────────────────────────────


# See top-of-file: `ReviewJob` is imported from `domain/reviewer/review_job.py`.


# `get_review_job`, `list_review_jobs_for_pr`, `list_in_flight`, and
# `metrics_summary` moved to `domain/reviewer/review_job_queries.py`
# (slice 44). Re-imported at the top of the file under the same names so
# in-flight callers don't break.


def _findingdrafts_to_raw(
    drafts: list[FindingDraft],
    *,
    commit_sha: str,
    read_file: Callable[[str], list[str] | None],
    source_agent: str = "coding_agent",
) -> list[RawFinding]:
    """Thin shim — the real implementation now lives in
    `domain/reviewer/admission.findingdrafts_to_raw`. Kept here so legacy
    queue + incremental callsites keep working until they migrate; the
    M05 `PostFindings` WorkflowCommand should import from `admission`
    directly."""
    from app.domain.reviewer.admission import findingdrafts_to_raw  # noqa: PLC0415

    return findingdrafts_to_raw(
        drafts,
        commit_sha=commit_sha,
        read_file=read_file,
        source_agent=source_agent,
    )


def _raw_to_vcs_findings(raw: list[RawFinding], new_findings: list[Any]) -> list[Any]:
    """Thin shim — real implementation now lives in
    `domain/reviewer/admission.raw_to_vcs_findings`. Kept here for the
    legacy `_run_review_job_inner` callsite + the incremental.py callsite;
    the M05 `PostFindings` GitHub-post follow-on imports from `admission`
    directly."""
    from app.domain.reviewer.admission import raw_to_vcs_findings  # noqa: PLC0415

    return raw_to_vcs_findings(raw, new_findings)


async def startup_recovery() -> None:
    """Mark any `running` jobs from a prior process as failed; respawn `queued` jobs."""
    async with db_session() as s:
        crashed = (await s.execute(select(ReviewRow.id).where(ReviewRow.status == "running"))).scalars().all()
        if crashed:
            await s.execute(
                update(ReviewRow)
                .where(ReviewRow.status == "running")
                .values(
                    status="failed",
                    skip_reason="crashed",
                    completed_at=_utcnow(),
                    error_message="process crashed mid-execution",
                )
            )
        queued = (await s.execute(select(ReviewRow).where(ReviewRow.status == "queued"))).scalars().all()
        for jid in crashed:
            await audit_for_review_job(
                jid,
                "review_job.failed",
                _FailedPayload(
                    invocation_status="crashed",
                    error="yaaos restarted during execution",
                    raw_output_excerpt="",
                ),
                actor=Actor.system(),
                org_id=M01_ORG_ID,
                session=s,
            )
        await s.commit()

    # Resolve ticket_id per queued job (via the PR row) so we can respawn.
    from app.domain.pull_requests.models import PullRequestRow  # noqa: PLC0415

    for row in queued:
        async with db_session() as s:
            pr_row = (
                await s.execute(select(PullRequestRow).where(PullRequestRow.id == row.pr_id))
            ).scalar_one_or_none()
        if pr_row is None:
            continue
        spawn(
            f"review_job:{row.id}",
            _run_review_job_with_context(
                ReviewJobInput(
                    review_job_id=row.id,
                    ticket_id=pr_row.ticket_id,
                    org_id=row.org_id,
                    debounce_seconds=0,
                )
            ),
        )
