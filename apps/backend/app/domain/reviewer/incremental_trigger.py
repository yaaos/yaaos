"""Push-driven incremental review: trigger policy + engine dispatch.

Owns:

- `start_incremental_review` — entry point called by intake on
  `pull_request synchronize` and on `@yaaos review` comments. Runs the
  trigger policy. On `Skip`/`Debounce`: returns the reason. On `Run`:
  creates a `ReviewRow` (for the SPA's per-PR history UI) and dispatches
  an `incremental_review_v1` workflow_execution via `core/workflow.engine`.
  The `IncrementalReview` engine command in `commands/__init__.py`
  consumes the same `(review_id, prev_sha, head_sha)` payload to run the
  actual review.

- Trigger-input helpers (`_last_reviewed_sha`, `_in_flight_review_id`,
  `_last_push_timestamp`, `_new_commit_messages`, `_is_ancestor`) used
  by `decide_trigger`.

- `_create_incremental_review` / `_fail_review` / `_skip_review` —
  ReviewRow lifecycle helpers shared with the engine command (kept
  alongside the trigger so PR-history bookkeeping lives in one place).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from sqlalchemy import desc, select, update

from app.core.database import session as db_session
from app.core.observability import spawn
from app.domain import pull_requests, tickets
from app.domain.reviewer.constants import DEFAULT_EFFORT as _DEFAULT_EFFORT
from app.domain.reviewer.constants import DEFAULT_MODEL as _DEFAULT_MODEL
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.models import ReviewRow
from app.domain.reviewer.trigger import (
    Debounce,
    Run,
    Skip,
    TriggerInputs,
    decide_trigger,
    humanize_skip,
)
from app.domain.vcs import get_plugin as get_vcs_plugin

log = structlog.get_logger("reviewer.incremental_trigger")

_DEBOUNCE_WINDOW_SECONDS = 30


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def start_incremental_review(
    pr_id: UUID,
    *,
    new_head_sha: str,
    prev_head_sha: str | None,
    org_id: UUID,
) -> str | None:
    """Engine-shaped entry point for push-driven incremental review.

    Runs the trigger policy. On `Skip`, returns the reason. On `Debounce`,
    spawns a delayed re-check. On `Run`, creates a `ReviewRow` (for the SPA's
    per-PR history UI) and dispatches an `incremental_review_v1` workflow
    via `core/workflow.engine`. The engine's `IncrementalReview` command
    runs the actual review against the engine-provisioned workspace.

    Returns: `"scheduled"`, `"skipped:<reason>"`, or `"debounced:<seconds>"`.
    """
    pr = await pull_requests.get(pr_id, org_id=org_id)
    last_reviewed_sha = await _last_reviewed_sha(pr_id)
    in_flight_id = await _in_flight_review_id(pr_id)
    last_push_at = await _last_push_timestamp(pr_id)

    effective_prev = last_reviewed_sha or prev_head_sha
    ancestor_ok = await _is_ancestor(pr.plugin_id, pr.repo_external_id, effective_prev, new_head_sha)
    new_commit_messages = await _new_commit_messages(
        pr.plugin_id, pr.repo_external_id, effective_prev, new_head_sha
    )

    inputs = TriggerInputs(
        pr_is_draft=pr.is_draft,
        last_reviewed_sha=effective_prev,
        head_sha=new_head_sha,
        in_flight_review_id=str(in_flight_id) if in_flight_id else None,
        new_commit_messages=new_commit_messages,
        last_reviewed_sha_is_ancestor=ancestor_ok,
        last_push_at=last_push_at,
        now=_utcnow(),
        debounce_window_seconds=_DEBOUNCE_WINDOW_SECONDS,
    )

    decision = decide_trigger(inputs)
    if isinstance(decision, Skip):
        log.info(
            "incremental.skipped",
            pr_id=str(pr_id),
            reason=decision.reason,
            human=humanize_skip(decision.reason),
        )
        if decision.reason == "in_flight" and in_flight_id is not None:
            async with db_session() as s:
                await s.execute(
                    update(ReviewRow).where(ReviewRow.id == in_flight_id).values(pending_replay=True)
                )
                await s.commit()
        return f"skipped:{decision.reason}"

    if isinstance(decision, Debounce):
        log.info(
            "incremental.debounced",
            pr_id=str(pr_id),
            seconds_remaining=decision.seconds_remaining,
        )
        spawn(
            f"incremental_debounce:{pr_id}:{new_head_sha[:8]}",
            _debounce_then_retry(
                pr_id=pr_id,
                new_head_sha=new_head_sha,
                prev_head_sha=prev_head_sha,
                org_id=org_id,
                delay=decision.seconds_remaining,
            ),
        )
        return f"debounced:{int(decision.seconds_remaining)}"

    assert isinstance(decision, Run)
    review_id = await _create_incremental_review(
        pr_id=pr_id,
        org_id=org_id,
        prev_sha=decision.scope.base_sha,
        head_sha=decision.scope.head_sha,
    )
    ticket = await tickets.get_by_pr(pr_id, org_id=org_id)
    if ticket is None:
        log.warning("incremental.no_ticket", pr_id=str(pr_id))
        await _fail_review(review_id, "no_ticket")
        return "skipped:no_ticket"

    from app.core.workflow import get_engine  # noqa: PLC0415

    async with db_session() as s:
        await get_engine().start(
            workflow_name="incremental_review_v1",
            ticket_id=str(ticket.id),
            ticket_payload={
                "review_id": str(review_id),
                "pr_id": str(pr.id),
                "pr_external_id": pr.external_id,
                "prev_sha": decision.scope.base_sha,
                "head_sha": decision.scope.head_sha,
                "base_sha": decision.scope.base_sha,
            },
            session=s,
        )
        await s.commit()
    return "scheduled"


# Alias for callers / tests that use the `handle_push` name.
handle_push = start_incremental_review


async def _debounce_then_retry(
    *, pr_id: UUID, new_head_sha: str, prev_head_sha: str | None, org_id: UUID, delay: float
) -> None:
    await asyncio.sleep(max(0.0, delay))
    await start_incremental_review(
        pr_id, new_head_sha=new_head_sha, prev_head_sha=prev_head_sha, org_id=org_id
    )


async def _create_incremental_review(*, pr_id: UUID, org_id: UUID, prev_sha: str, head_sha: str) -> UUID:
    """Insert the per-PR-history `ReviewRow` and return its id."""
    from sqlalchemy import func as sa_func  # noqa: PLC0415

    new_id = uuid4()
    async with db_session() as s:
        await acquire_pr_lock(s, pr_id)
        max_seq = (
            await s.execute(
                select(sa_func.coalesce(sa_func.max(ReviewRow.sequence_number), 0)).where(
                    ReviewRow.pr_id == pr_id
                )
            )
        ).scalar_one()
        s.add(
            ReviewRow(
                id=new_id,
                org_id=org_id,
                pr_id=pr_id,
                sequence_number=max_seq + 1,
                status="queued",
                trigger_reason="push_incremental",
                destination="vcs",
                scope_kind="incremental",
                scope_prev_sha=prev_sha,
                commit_sha_at_start=head_sha,
                model=_DEFAULT_MODEL,
                effort=_DEFAULT_EFFORT,
            )
        )
        await s.commit()
    return new_id


async def set_review_step(review_id: UUID, step: str) -> None:
    """Update `current_step` + bump heartbeat. Public helper for the engine
    command + future engine steps."""
    async with db_session() as s:
        await s.execute(
            update(ReviewRow)
            .where(ReviewRow.id == review_id)
            .values(current_step=step, last_heartbeat_at=_utcnow())
        )
        await s.commit()


async def fail_review(review_id: UUID, error: str) -> None:
    """Transition the ReviewRow to `failed`."""
    async with db_session() as s:
        await s.execute(
            update(ReviewRow)
            .where(ReviewRow.id == review_id)
            .values(
                status="failed",
                completed_at=_utcnow(),
                error_message=error,
                current_step="failed",
            )
        )
        await s.commit()


async def skip_review(review_id: UUID, reason: str) -> None:
    """Transition the ReviewRow to `skipped`."""
    async with db_session() as s:
        await s.execute(
            update(ReviewRow)
            .where(ReviewRow.id == review_id)
            .values(status="skipped", skip_reason=reason, completed_at=_utcnow())
        )
        await s.commit()


# Internal aliases used by the engine command body.
_fail_review = fail_review
_skip_review = skip_review


# ── Trigger-input helpers ─────────────────────────────────────────────


async def _last_reviewed_sha(pr_id: UUID) -> str | None:
    """The commit_sha_at_start of the most-recent posted review for this PR."""
    async with db_session() as s:
        row = (
            await s.execute(
                select(ReviewRow.commit_sha_at_start)
                .where(ReviewRow.pr_id == pr_id, ReviewRow.status == "posted")
                .order_by(desc(ReviewRow.created_at))
                .limit(1)
            )
        ).first()
    return row[0] if row else None


async def _in_flight_review_id(pr_id: UUID) -> UUID | None:
    async with db_session() as s:
        row = (
            await s.execute(
                select(ReviewRow.id)
                .where(ReviewRow.pr_id == pr_id, ReviewRow.status.in_(["queued", "running"]))
                .limit(1)
            )
        ).first()
    return row[0] if row else None


async def _last_push_timestamp(pr_id: UUID) -> datetime | None:
    async with db_session() as s:
        row = (
            await s.execute(
                select(ReviewRow.scheduled_at)
                .where(
                    ReviewRow.pr_id == pr_id,
                    ReviewRow.trigger_reason.in_(["push_incremental", "pr_synchronized"]),
                )
                .order_by(desc(ReviewRow.scheduled_at))
                .limit(1)
            )
        ).first()
    return row[0] if row else None


async def _new_commit_messages(
    plugin_id: str, repo_external_id: str, prev_sha: str | None, head_sha: str
) -> list[str]:
    if not prev_sha or prev_sha == head_sha:
        return []
    plugin = get_vcs_plugin(plugin_id)
    try:
        return await plugin.list_commit_messages(repo_external_id, prev_sha, head_sha)
    except Exception:
        log.warning(
            "incremental.list_commit_messages_failed",
            repo=repo_external_id,
            prev=prev_sha,
            head=head_sha,
        )
        return []


async def _is_ancestor(plugin_id: str, repo_external_id: str, prev_sha: str | None, head_sha: str) -> bool:
    if not prev_sha:
        return False
    if prev_sha == head_sha:
        return True
    plugin = get_vcs_plugin(plugin_id)
    try:
        force_push = await plugin.detect_force_push(repo_external_id, prev_sha, head_sha)
        return not force_push
    except Exception:
        log.warning("incremental.is_ancestor_failed", repo=repo_external_id, prev=prev_sha, head=head_sha)
        return False


__all__ = [
    "fail_review",
    "handle_push",
    "set_review_step",
    "skip_review",
    "start_incremental_review",
]
