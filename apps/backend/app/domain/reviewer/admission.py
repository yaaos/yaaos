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

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.coding_agent import FindingDraft
from app.domain.reviewer.aggregate import AdmissionDrop, RawFinding
from app.domain.reviewer.anchor import make_anchor
from app.domain.reviewer.fingerprint import compute_fingerprint
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.types import Finding, FindingObservation

log = structlog.get_logger("reviewer.admission")


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


def findingdrafts_to_raw(
    drafts: list[FindingDraft],
    *,
    commit_sha: str,
    read_file: Callable[[str], list[str] | None],
    source_agent: str = "coding_agent",
) -> list[RawFinding]:
    """Convert §10.1 `FindingDraft`s from the coding agent into `RawFinding`s.

    Plan §2.3: anchor + fingerprint hashes use real file content at the
    anchored line range — never the body text. Two findings at the same
    file:line with different body phrasings must produce IDENTICAL
    fingerprints so the aggregate deduplicates re-observations across
    reviews. Drafts whose file we can't read are dropped — no stable
    fingerprint without real content.

    Shared between full review, incremental review, and the M05 PostFindings
    `WorkflowCommand` body (post queue.py dismantle). The `read_file`
    callback typically wraps `workspace.read_text()`.
    """
    out: list[RawFinding] = []
    for d in drafts:
        file_lines = read_file(d.anchor.file_path)
        # `None` = file missing; `[]` = file present but empty. Both fail the
        # same way — no stable anchor / fingerprint without real content.
        if not file_lines:
            log.info(
                "review.findingdraft_dropped_no_file",
                file=d.anchor.file_path,
                rule_id=d.rule_id,
            )
            continue
        # Defensive clamp — plan §10.1 enforces a valid range on the agent
        # but we don't want make_anchor to raise on off-by-one drafts.
        ls = max(1, min(d.anchor.line_start, len(file_lines)))
        le = max(ls, min(d.anchor.line_end, len(file_lines)))
        anchor = make_anchor(
            file_path=d.anchor.file_path,
            file_lines=file_lines,
            line_start=ls,
            line_end=le,
            commit_sha=commit_sha,
        )
        anchored_lines = file_lines[ls - 1 : le]
        fingerprint = compute_fingerprint(
            file_path=d.anchor.file_path,
            rule_id=d.rule_id,
            anchored_lines=anchored_lines,
            title=d.title,
        )
        out.append(
            RawFinding(
                fingerprint=fingerprint,
                rule_id=d.rule_id,
                title=d.title,
                body=d.body,
                rationale=d.rationale,
                concrete_failure_scenario=d.concrete_failure_scenario,
                confidence=d.confidence,
                severity=d.severity,
                anchor=anchor,
                source_agent=source_agent,
                duplicate_of_rule_ids=d.duplicate_of_rule_ids,
            )
        )
    return out


__all__ = ["AdmissionResult", "admit_raw_findings", "findingdrafts_to_raw"]
