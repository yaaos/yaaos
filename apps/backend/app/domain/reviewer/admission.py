"""Admission gate — the single entry point for routing raw coding-agent
findings through the aggregate's filter pipeline.

`aggregate.post_process_raw_findings` already implements the gate logic
(schema → threshold → off-diff → nit cap → cross-file dedup → top-10 cap →
dedup vs prior). `admit_raw_findings` wraps it with the repo lifecycle —
loads the aggregate, runs the gate, saves the survivors — and returns a
structured `AdmissionResult` callers can inspect.

Used by the `PostFindings` WorkflowCommand body. Consolidates the
repository + aggregate `save()` lifecycle into one place so callers
don't need to know about either.
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
from app.domain.reviewer.types import Finding, FindingObservation, Severity

# Severity tier collapse: the four reviewer severities mapped onto the
# three-tier vcs.Finding enum the GitHub plugin posts as.
_SEVERITY_TO_VCS: dict[Severity, str] = {
    "blocker": "must-fix",
    "major": "must-fix",
    "minor": "suggestion",
    "nit": "nit",
}

log = structlog.get_logger("reviewer.admission")


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of running `admit_raw_findings`. Callers can audit `drops`,
    post `admitted` to GitHub, persist `observations` against the
    review_jobs table, etc."""

    admitted: list[Finding]
    observations: list[FindingObservation]
    drops: list[AdmissionDrop]


async def admit_raw_findings(
    *,
    pr_id: UUID,
    org_id: UUID,
    raw: list[RawFinding],
    commit_sha: str,
    trigger: str = "scheduled_full",
    scope: str = "full",
    diff_files: set[str] | None = None,
    session: AsyncSession,
) -> AdmissionResult:
    """Load the PR's review aggregate, open a new `Review` row, run
    admission against `raw`, persist the survivors, return the structured
    result. Required `session` — caller commits; aggregate writes are
    flushed but not committed here.

    Opening a review (rather than accepting a pre-built review_id) keeps
    relational integrity automatic: the `findings` table's FK to `reviews`
    is satisfied by the row this function inserts. The aggregate emits
    the matching `ReviewRequested` event.

    `commit_sha`: head_sha at review start; stored on the Review row.
    `trigger`: `coding_agent.ReviewTrigger` literal (default `scheduled_full`
    matches the `pr_review_v1` full-review path).
    `scope`: `coding_agent.ReviewScope` literal (default `full`).
    `diff_files`: when supplied, the gate drops findings whose anchor file
    isn't in the set (off-diff suppression). Pass None for
    full-PR review paths that don't have a narrow diff scope.
    """
    if not raw:
        # No drafts to admit → no review needed. Skip the start_review +
        # save so callers without a real pr_id row (smoke tests, empty
        # CodeReview output) don't trip the FK.
        return AdmissionResult(admitted=[], observations=[], drops=[])

    repo = SqlAlchemyAggregateRepository(session)
    aggregate = await repo.load(pr_id=pr_id, org_id=org_id)
    review = aggregate.start_review(
        trigger=trigger,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        commit_sha=commit_sha,
    )
    admitted, observations, drops = aggregate.post_process_raw_findings(review.id, raw, diff_files=diff_files)
    await repo.save(aggregate)

    log.info(
        "admission.done",
        pr_id=str(pr_id),
        org_id=str(org_id),
        review_id=str(review.id),
        raw_in=len(raw),
        admitted=len(admitted),
        dropped=len(drops),
        trigger=trigger,
        scope=scope,
    )
    if drops:
        # Per-drop info goes to structured logs.
        log.info(
            "admission.drops",
            pr_id=str(pr_id),
            review_id=str(review.id),
            drops=[
                {
                    "rule_id": d.rule_id,
                    "reason": d.reason,
                    "severity": d.severity,
                    "confidence": d.confidence,
                }
                for d in drops
            ],
        )
    return AdmissionResult(admitted=admitted, observations=observations, drops=drops)


def findingdrafts_to_raw(
    drafts: list[FindingDraft],
    *,
    commit_sha: str,
    read_file: Callable[[str], list[str] | None],
    source_agent: str = "coding_agent",
) -> list[RawFinding]:
    """Convert `FindingDraft`s from the coding agent into `RawFinding`s.

    Anchor + fingerprint hashes use real file content at the
    anchored line range — never the body text. Two findings at the same
    file:line with different body phrasings must produce IDENTICAL
    fingerprints so the aggregate deduplicates re-observations across
    reviews. Drafts whose file we can't read are dropped — no stable
    fingerprint without real content.

    Shared between full review, incremental review, and the PostFindings
    `WorkflowCommand` body. The `read_file` callback typically wraps
    `workspace.read_text()`.
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
        # Defensive clamp — the agent is required to emit a valid range,
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


def raw_to_vcs_findings(
    raw: list[RawFinding],
    admitted: list[Finding],
):  # type: ignore[no-untyped-def]
    """Map admitted `RawFinding`s back into `vcs.Finding` payloads for
    posting to GitHub (or whichever VCS plugin owns the PR).

    Only admitted findings (post-aggregate-gate) translate; rejected ones
    never reach the VCS plugin. Severity collapses the four reviewer tiers
    onto the VCS three-tier enum via `_SEVERITY_TO_VCS`.

    Used by the `PostFindings` GitHub-posting command.
    """
    from app.domain.vcs import Finding as VcsFinding  # noqa: PLC0415

    out: list[VcsFinding] = []
    admitted_fps = {f.fingerprint.hash for f in admitted}
    for r in raw:
        if r.fingerprint.hash not in admitted_fps:
            continue
        out.append(
            VcsFinding(
                file=r.anchor.file_path,
                line_start=r.anchor.line_start,
                line_end=r.anchor.line_end,
                severity=_SEVERITY_TO_VCS.get(r.severity, "suggestion"),
                title=r.title,
                body=r.body,
                rationale=r.rationale,
                snippet=None,
                applied_lesson_ids=[],
                source_agent=r.source_agent,
            )
        )
    return out


async def post_admitted_findings_to_vcs(
    *,
    pr_id: UUID,
    org_id: UUID,
    pr_external_id: str,
    vcs_plugin_id: str,
    admitted,  # type: ignore[no-untyped-def]
    raw: list[RawFinding],
    summary_body: str | None,
    state: str = "COMMENT",
    agent_tag: str = "yaaos",
    session: AsyncSession,
):  # type: ignore[no-untyped-def]
    """Post admitted findings to the VCS plugin AND attach yaaos
    `CommentMessage`s to each finding's thread using the returned
    external_comment_ids.

    Args:
    - `pr_id`, `org_id`: the aggregate scope.
    - `pr_external_id`: the GitHub PR id (or other VCS-side id) the plugin
      posts the review against.
    - `vcs_plugin_id`: which plugin to dispatch to (typically `"github"`).
      Tests register a `StubVCSPlugin` under the same id.
    - `admitted`: the list of admitted `Finding` rows from `admit_raw_findings`.
      Used to determine which findings need threads.
    - `raw`: the original `RawFinding` list — needed by `raw_to_vcs_findings`
      to translate admitted fingerprints back to `vcs.Finding` payloads.
    - `summary_body`: top-level review body. May be None.
    - `state`, `agent_tag`: vcs.Review fields.

    Returns the `ReviewPostResult` from the VCS plugin so callers can
    record metrics (tokens, latency, external_id).

    NOT idempotent — calling twice will post twice. The aggregate's thread
    machinery prevents duplicate yaaos messages on the same external_comment_id
    but the GitHub side has no dedup. Callers must ensure single-flight.
    """
    from app.domain.reviewer.repository import (  # noqa: PLC0415
        SqlAlchemyAggregateRepository,
    )
    from app.domain.vcs import Review as VcsReview  # noqa: PLC0415
    from app.domain.vcs import get_plugin as get_vcs_plugin  # noqa: PLC0415

    plugin = get_vcs_plugin(vcs_plugin_id)
    posted_vcs_findings = raw_to_vcs_findings(raw, admitted)

    review_obj = VcsReview(
        agent_tag=agent_tag,
        state=state,  # type: ignore[arg-type]
        summary_body=summary_body,
        findings=posted_vcs_findings,
    )
    post_result = await plugin.post_review(pr_external_id, review_obj)

    # Attach yaaos messages to each finding's thread using the returned
    # external_comment_ids. Map back by index — the plugin's mapping is
    # keyed by vcs.Finding index (which matches `admitted` order since
    # raw_to_vcs_findings preserves order).
    repo = SqlAlchemyAggregateRepository(session)
    aggregate = await repo.load(pr_id=pr_id, org_id=org_id)
    external_ids = list(post_result.finding_to_comment_external_id.values())
    for idx, f in enumerate(admitted):
        external_id = external_ids[idx] if idx < len(external_ids) else f"local-{f.id}"
        thread = aggregate.open_thread_for_finding(f.id)
        aggregate.append_message(
            thread_id=thread.id,
            author_kind="yaaos",
            author_external_id=agent_tag,
            external_comment_id=external_id,
            body=f.body,
        )
    await repo.save(aggregate)

    log.info(
        "vcs_post.done",
        pr_id=str(pr_id),
        org_id=str(org_id),
        pr_external_id=pr_external_id,
        review_external_id=post_result.review_external_id,
        findings_posted=len(posted_vcs_findings),
    )
    return post_result


__all__ = [
    "AdmissionResult",
    "admit_raw_findings",
    "findingdrafts_to_raw",
    "post_admitted_findings_to_vcs",
    "raw_to_vcs_findings",
]
