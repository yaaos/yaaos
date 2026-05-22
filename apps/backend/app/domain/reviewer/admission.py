"""Admission gate — the single entry point for routing raw coding-agent
findings through the aggregate's filter pipeline.

`aggregate.post_process_raw_findings` already implements the gate logic
(schema → threshold → off-diff → nit cap → cross-file dedup → top-10 cap →
dedup vs prior). `admit_raw_findings` wraps it with the repo lifecycle —
loads the aggregate, runs the gate, saves the survivors — and returns a
structured `AdmissionResult` callers can inspect.

Used by:
- `domain/reviewer/queue.py` (legacy `_run_review_job_inner`) — until the
  queue dismantle replaces it.
- The future `PostFindings` WorkflowCommand body (M05 Phase 4 follow-on
  alongside the queue.py dismantle).

Per the Phase 4 plan item, this consolidates what was previously inline
between `queue.py:769-834` and `aggregate.post_process_raw_findings` into
one place. Callers don't need to know about the repository or the
aggregate's `save()` lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reviewer.aggregate import AdmissionDrop, RawFinding
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.types import Finding, FindingObservation


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of running `admit_raw_findings`. Callers can audit `drops`,
    post `admitted` to GitHub, persist `observations` against the legacy
    review_jobs table (until the M05 dismantle), etc."""

    admitted: list[Finding]
    observations: list[FindingObservation]
    drops: list[AdmissionDrop]


async def admit_raw_findings(
    *,
    pr_id: UUID,
    org_id: UUID,
    review_id: UUID,
    raw: list[RawFinding],
    diff_files: set[str] | None = None,
    session: AsyncSession,
) -> AdmissionResult:
    """Load the PR's review aggregate, run admission against `raw`, persist
    the survivors, and return the structured result. Required `session` —
    caller commits; aggregate writes are flushed but not committed here.

    `review_id` is the identifier of the review run that produced `raw`.
    The aggregate attaches each `FindingObservation` to this id so future
    queries can trace which run first surfaced each finding.

    `diff_files`: when supplied, the gate drops findings whose anchor file
    isn't in the set (plan §10.9 off-diff suppression). Pass None for
    full-PR review paths that don't have a narrow diff scope.
    """
    repo = SqlAlchemyAggregateRepository(session)
    aggregate = await repo.load(pr_id=pr_id, org_id=org_id)
    admitted, observations, drops = aggregate.post_process_raw_findings(review_id, raw, diff_files=diff_files)
    await repo.save(aggregate)
    return AdmissionResult(admitted=admitted, observations=observations, drops=drops)


__all__ = ["AdmissionResult", "admit_raw_findings"]
