"""SQLAlchemy implementation of `AggregateRepository`.

Loads all rows tied to a PR in one transaction, builds a `PRReviewAggregate`,
and drains its pending writes back to the DB on `save`. The caller is
expected to hold a per-PR advisory lock (see `lock.py`) so concurrent
mutators serialize.

The implementation maps each row model to/from the matching dataclass in
`types.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.models import (
    AcknowledgmentDecisionRow,
    CommentMessageRow,
    CommentThreadRow,
    FindingObservationRow,
    FindingRow,
    ReviewRow,
)
from app.domain.reviewer.types import (
    AcknowledgmentDecision,
    CodeAnchor,
    CommentMessage,
    CommentThread,
    Finding,
    FindingFingerprint,
    FindingObservation,
    FindingState,
    Review,
    ReviewScope,
    ReviewScopeKind,
    ReviewTrigger,
)


def _anchor_to_jsonb(a: CodeAnchor) -> dict[str, Any]:
    return {
        "file_path": a.file_path,
        "line_start": a.line_start,
        "line_end": a.line_end,
        "surrounding_content_hash": a.surrounding_content_hash,
        "commit_sha": a.commit_sha,
        "original_lines": list(a.original_lines),
    }


def _anchor_from_jsonb(d: dict[str, Any]) -> CodeAnchor:
    # `original_lines` is missing on rows that don't carry it — default to ().
    return CodeAnchor(
        file_path=d["file_path"],
        line_start=int(d["line_start"]),
        line_end=int(d["line_end"]),
        surrounding_content_hash=d["surrounding_content_hash"],
        commit_sha=d["commit_sha"],
        original_lines=tuple(d.get("original_lines") or ()),
    )


def _fingerprint_from_row(row: FindingRow) -> FindingFingerprint:
    # We persist the composite hash as `fingerprint_hash` and the inputs
    # individually in `current_anchor` JSONB / `rule_id` / `title`. Reconstruct
    # the FindingFingerprint from those — but its component hashes must already
    # equal what produced `fingerprint_hash` (verified at insert time).
    parts = row.fingerprint_hash.split("|")
    if len(parts) != 4:
        # Defensive: shouldn't happen if we always insert via the aggregate.
        return FindingFingerprint(
            file_path=row.current_anchor["file_path"],
            rule_id=row.rule_id,
            anchor_content_hash="",
            body_gist_hash="",
        )
    file_path, rule_id, anchor_hash, body_hash = parts
    return FindingFingerprint(
        file_path=file_path,
        rule_id=rule_id,
        anchor_content_hash=anchor_hash,
        body_gist_hash=body_hash,
    )


def _finding_from_row(row: FindingRow) -> Finding:
    return Finding(
        id=row.id,
        pr_id=row.pr_id,
        org_id=row.org_id,
        fingerprint=_fingerprint_from_row(row),
        rule_id=row.rule_id,
        title=row.title,
        body=row.body,
        rationale=row.rationale,
        concrete_failure_scenario=row.concrete_failure_scenario,
        confidence=row.confidence,
        severity=row.severity,  # type: ignore[arg-type]
        state=FindingState(row.state),
        current_anchor=_anchor_from_jsonb(row.current_anchor),
        source_agent=row.source_agent,
        first_seen_review_id=row.first_seen_review_id,
        last_observed_review_id=row.last_observed_review_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _observation_from_row(row: FindingObservationRow) -> FindingObservation:
    return FindingObservation(
        id=row.id,
        finding_id=row.finding_id,
        review_id=row.review_id,
        anchor=_anchor_from_jsonb(row.anchor),
        raw_body=row.raw_body,
        created_at=row.created_at,
    )


def _thread_from_row(row: CommentThreadRow) -> CommentThread:
    return CommentThread(
        id=row.id,
        finding_id=row.finding_id,
        external_thread_id=row.external_thread_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _message_from_row(row: CommentMessageRow) -> CommentMessage:
    return CommentMessage(
        id=row.id,
        thread_id=row.thread_id,
        author_kind=row.author_kind,  # type: ignore[arg-type]
        author_external_id=row.author_external_id,
        external_comment_id=row.external_comment_id,
        in_reply_to_external_id=row.in_reply_to_external_id,
        body=row.body,
        classified_intent=row.classified_intent,  # type: ignore[arg-type]
        created_at=row.created_at,
    )


def _review_from_row(row: ReviewRow) -> Review:
    scope = ReviewScope(
        kind=ReviewScopeKind(row.scope_kind),
        base_sha=row.scope_prev_sha or "",
        head_sha=row.commit_sha_at_start or "",
    )
    # `trigger_reason` accepts the canonical vocabulary plus other values; the
    # ReviewTrigger enum covers the canonical set, anything else maps to
    # PR_READY for in-memory representation (the DB column keeps the real value).
    try:
        trigger = ReviewTrigger(row.trigger_reason)
    except ValueError:
        trigger = ReviewTrigger.PR_READY
    return Review(
        id=row.id,
        pr_id=row.pr_id,
        org_id=row.org_id,
        sequence_number=row.sequence_number,
        trigger_reason=trigger,
        scope=scope,
        commit_sha_at_start=row.commit_sha_at_start or "",
        status=row.status,
        superseded_by_review_id=row.superseded_by_review_id,
        pending_replay=row.pending_replay,
        created_at=row.created_at,
    )


def _ack_from_row(row: AcknowledgmentDecisionRow) -> AcknowledgmentDecision:
    return AcknowledgmentDecision(
        id=row.id,
        finding_id=row.finding_id,
        kind=row.kind,  # type: ignore[arg-type]
        rationale=row.rationale,
        made_by_external_id=row.made_by_external_id,
        made_by_message_id=row.made_by_message_id,
        created_at=row.created_at,
    )


class SqlAlchemyAggregateRepository:
    """`AggregateRepository` impl backed by an `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, *, pr_id: uuid.UUID, org_id: uuid.UUID) -> PRReviewAggregate:
        review_rows = list(
            (
                await self._session.execute(
                    select(ReviewRow).where(ReviewRow.pr_id == pr_id, ReviewRow.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        finding_rows = (
            (
                await self._session.execute(
                    select(FindingRow).where(FindingRow.pr_id == pr_id, FindingRow.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        finding_ids = [f.id for f in finding_rows]
        observation_rows: list[FindingObservationRow] = []
        thread_rows: list[CommentThreadRow] = []
        message_rows: list[CommentMessageRow] = []
        ack_rows: list[AcknowledgmentDecisionRow] = []
        if finding_ids:
            observation_rows = list(
                (
                    await self._session.execute(
                        select(FindingObservationRow).where(FindingObservationRow.finding_id.in_(finding_ids))
                    )
                )
                .scalars()
                .all()
            )
            thread_rows = list(
                (
                    await self._session.execute(
                        select(CommentThreadRow).where(CommentThreadRow.finding_id.in_(finding_ids))
                    )
                )
                .scalars()
                .all()
            )
            thread_ids = [t.id for t in thread_rows]
            if thread_ids:
                message_rows = list(
                    (
                        await self._session.execute(
                            select(CommentMessageRow)
                            .where(CommentMessageRow.thread_id.in_(thread_ids))
                            .order_by(CommentMessageRow.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
            ack_rows = list(
                (
                    await self._session.execute(
                        select(AcknowledgmentDecisionRow).where(
                            AcknowledgmentDecisionRow.finding_id.in_(finding_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )

        return PRReviewAggregate(
            pr_id=pr_id,
            org_id=org_id,
            reviews=[_review_from_row(r) for r in review_rows],
            findings=[_finding_from_row(r) for r in finding_rows],
            observations=[_observation_from_row(r) for r in observation_rows],
            threads=[_thread_from_row(r) for r in thread_rows],
            messages=[_message_from_row(r) for r in message_rows],
            acks=[_ack_from_row(r) for r in ack_rows],
        )

    async def save(self, aggregate: PRReviewAggregate) -> None:
        pending = aggregate.pop_pending()

        # Reviews. Some callers INSERT the ReviewRow externally before
        # kicking off the runner; the aggregate mutations (mark_review_running,
        # complete_review, supersede_review, set_pending_replay) flow through
        # here as UPDATEs to an existing row. The `admission.admit_raw_findings`
        # path calls
        # `aggregate.start_review` and expects the repo to materialize the
        # row — so when `pending.new_reviews` carries an id that's NOT yet
        # in the DB, we INSERT it. Existing rows fall through to the UPDATE
        # branch unchanged.
        for r in pending.new_reviews:
            row = await self._session.get(ReviewRow, r.id)
            if row is None:
                self._session.add(
                    ReviewRow(
                        id=r.id,
                        org_id=r.org_id,
                        pr_id=r.pr_id,
                        sequence_number=r.sequence_number,
                        trigger_reason=r.trigger_reason,
                        scope_kind=(r.scope.kind.value if isinstance(r.scope, ReviewScope) else str(r.scope)),
                        scope_prev_sha=(
                            r.scope.base_sha
                            if isinstance(r.scope, ReviewScope)
                            and r.scope.kind == ReviewScopeKind.INCREMENTAL
                            else None
                        ),
                        commit_sha_at_start=r.commit_sha_at_start,
                        status=r.status,
                        superseded_by_review_id=r.superseded_by_review_id,
                        pending_replay=r.pending_replay,
                    )
                )
            else:
                row.status = r.status
                row.commit_sha_at_start = r.commit_sha_at_start
                row.superseded_by_review_id = r.superseded_by_review_id
                row.pending_replay = r.pending_replay
                if r.status == "running" and row.started_at is None:
                    row.started_at = r.created_at
                if r.status in {"done", "failed", "superseded"} and row.completed_at is None:
                    row.completed_at = r.created_at

        for r in pending.updated_reviews:
            row = await self._session.get(ReviewRow, r.id)
            if row is None:
                # An UPDATE for an id never inserted is a bug; skip rather
                # than blow up to preserve the "tests insert externally"
                # escape hatch.
                continue
            row.status = r.status
            row.commit_sha_at_start = r.commit_sha_at_start
            row.superseded_by_review_id = r.superseded_by_review_id
            row.pending_replay = r.pending_replay
            if r.status == "running" and row.started_at is None:
                row.started_at = r.created_at
            if r.status in {"done", "failed", "superseded"} and row.completed_at is None:
                row.completed_at = r.created_at

        # Flush so new ReviewRows exist before the dependent findings INSERTs
        # reference them (findings.first_seen_review_id has a FK to reviews.id).
        await self._session.flush()

        # Findings + observations need FK ordering: findings first, flush so
        # PG sees the row, then observations + threads + messages + acks.
        for f in pending.new_findings:
            self._session.add(
                FindingRow(
                    id=f.id,
                    org_id=f.org_id,
                    pr_id=f.pr_id,
                    fingerprint_hash=f.fingerprint.hash,
                    rule_id=f.rule_id,
                    title=f.title,
                    body=f.body,
                    rationale=f.rationale,
                    concrete_failure_scenario=f.concrete_failure_scenario,
                    confidence=f.confidence,
                    severity=f.severity,
                    state=f.state.value,
                    current_anchor=_anchor_to_jsonb(f.current_anchor),
                    source_agent=f.source_agent,
                    first_seen_review_id=f.first_seen_review_id,
                    last_observed_review_id=f.last_observed_review_id,
                )
            )

        for f in pending.updated_findings:
            row = await self._session.get(FindingRow, f.id)
            if row is None:
                continue
            row.confidence = f.confidence
            row.state = f.state.value
            row.current_anchor = _anchor_to_jsonb(f.current_anchor)
            row.last_observed_review_id = f.last_observed_review_id

        # Flush so the new findings exist before dependent rows reference
        # them via FK (observations.finding_id, threads.finding_id, etc.).
        if pending.new_findings:
            await self._session.flush()

        for o in pending.new_observations:
            self._session.add(
                FindingObservationRow(
                    id=o.id,
                    finding_id=o.finding_id,
                    review_id=o.review_id,
                    anchor=_anchor_to_jsonb(o.anchor),
                    raw_body=o.raw_body,
                )
            )

        for t in pending.new_threads:
            self._session.add(
                CommentThreadRow(
                    id=t.id,
                    finding_id=t.finding_id,
                    external_thread_id=t.external_thread_id,
                )
            )

        # Flush threads before messages so message_thread_id FKs resolve.
        if pending.new_threads:
            await self._session.flush()

        for m in pending.new_messages:
            self._session.add(
                CommentMessageRow(
                    id=m.id,
                    thread_id=m.thread_id,
                    author_kind=m.author_kind,
                    author_external_id=m.author_external_id,
                    external_comment_id=m.external_comment_id,
                    in_reply_to_external_id=m.in_reply_to_external_id,
                    body=m.body,
                    classified_intent=m.classified_intent,
                )
            )

        # Flush messages so ack.made_by_message_id FKs resolve.
        if pending.new_messages:
            await self._session.flush()

        for a in pending.new_acks:
            self._session.add(
                AcknowledgmentDecisionRow(
                    id=a.id,
                    finding_id=a.finding_id,
                    kind=a.kind,
                    rationale=a.rationale,
                    made_by_external_id=a.made_by_external_id,
                    made_by_message_id=a.made_by_message_id,
                )
            )

        await self._session.flush()
