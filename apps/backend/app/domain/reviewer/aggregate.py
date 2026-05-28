"""`PRReviewAggregate` — the consistency boundary for one PR's review state.

Owns: all `Review`s, `Finding`s, `FindingObservation`s, `CommentThread`s,
`CommentMessage`s, and `AcknowledgmentDecision`s for the PR. External callers
never touch a `Finding` directly — everything goes through aggregate methods.

The aggregate is **pure**: it takes data in, produces decisions + events out.
It does not load itself from the DB (the repository does that) and it does
not persist itself (the repository does that too). Tests construct it
directly with in-memory state.

Concurrency: the aggregate is single-threaded by design. `service.py` takes
a per-PR PG advisory lock before loading, so two webhook events
for the same PR serialize.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.reviewer import state_machine
from app.domain.reviewer.events import (
    AgentReplyPosted,
    CommentReplyReceived,
    DomainEvent,
    FindingAcknowledged,
    FindingAnchorUpdated,
    FindingRaised,
    FindingReObserved,
    FindingResolutionDetected,
    FindingStaleDetected,
    FindingStateChanged,
    ReviewCompleted,
    ReviewFailed,
    ReviewRequested,
    ReviewStarted,
    ReviewSuperseded,
)
from app.domain.reviewer.types import (
    AckKind,
    AcknowledgmentDecision,
    AuthorKind,
    CodeAnchor,
    CommentMessage,
    CommentThread,
    Finding,
    FindingFingerprint,
    FindingObservation,
    FindingState,
    ReplyIntent,
    Review,
    ReviewScope,
    ReviewTrigger,
    Severity,
)

# Per-severity post threshold.
_POST_THRESHOLD: dict[Severity, int] = {
    "blocker": 75,
    "major": 75,
    "minor": 85,
    "nit": 90,
}

# Per-severity weight for the per-review top-10 cap.
_SEVERITY_WEIGHT: dict[Severity, int] = {
    "blocker": 4,
    "major": 3,
    "minor": 2,
    "nit": 1,
}

_PER_PR_NIT_CAP = 5
_PER_REVIEW_TOP_CAP = 10

# `concrete_failure_scenario` must actually describe a failure, not a
# one-word stand-in. Enforced at the boundary so every agent-output
# adapter has to supply a real scenario.
_MIN_SCENARIO_LEN = 20


@dataclass
class RawFinding:
    """An ungated finding produced by a coding-agent task.

    The aggregate decides whether to admit it (schema/threshold/cap/dedup
    checks) and how to transition state. The mapping from
    `coding_agent.FindingDraft` happens in `queue._findingdrafts_to_raw`
    (shared by both the full-review and incremental-review paths).
    """

    fingerprint: FindingFingerprint
    rule_id: str
    title: str
    body: str
    rationale: str
    concrete_failure_scenario: str
    confidence: int
    severity: Severity
    anchor: CodeAnchor
    source_agent: str
    duplicate_of_rule_ids: list[str] = field(default_factory=list)


@dataclass
class AdmissionDrop:
    """Why a raw finding was rejected during post-processing. Used for audit logs."""

    rule_id: str
    reason: str  # malformed | below_threshold | nit_cap | top_cap | matches_ack
    severity: Severity
    confidence: int


@dataclass
class _PRReviewState:
    """Loaded state for one PR. Repository fills this; aggregate mutates it."""

    pr_id: uuid.UUID
    org_id: uuid.UUID
    reviews: dict[uuid.UUID, Review] = field(default_factory=dict)
    findings: dict[uuid.UUID, Finding] = field(default_factory=dict)
    observations: list[FindingObservation] = field(default_factory=list)
    threads: dict[uuid.UUID, CommentThread] = field(default_factory=dict)
    messages: list[CommentMessage] = field(default_factory=list)
    acks: list[AcknowledgmentDecision] = field(default_factory=list)


@dataclass
class _PendingWrites:
    """What the aggregate has changed since load. Repository drains this on save."""

    new_reviews: list[Review] = field(default_factory=list)
    updated_reviews: list[Review] = field(default_factory=list)
    new_findings: list[Finding] = field(default_factory=list)
    updated_findings: list[Finding] = field(default_factory=list)
    new_observations: list[FindingObservation] = field(default_factory=list)
    new_threads: list[CommentThread] = field(default_factory=list)
    new_messages: list[CommentMessage] = field(default_factory=list)
    new_acks: list[AcknowledgmentDecision] = field(default_factory=list)


class PRReviewAggregate:
    """Application-side handle on one PR's review state."""

    def __init__(
        self,
        *,
        pr_id: uuid.UUID,
        org_id: uuid.UUID,
        reviews: list[Review] | None = None,
        findings: list[Finding] | None = None,
        observations: list[FindingObservation] | None = None,
        threads: list[CommentThread] | None = None,
        messages: list[CommentMessage] | None = None,
        acks: list[AcknowledgmentDecision] | None = None,
        now: datetime | None = None,
    ) -> None:
        self._state = _PRReviewState(pr_id=pr_id, org_id=org_id)
        for r in reviews or []:
            self._state.reviews[r.id] = r
        for f in findings or []:
            self._state.findings[f.id] = f
        self._state.observations = list(observations or [])
        for t in threads or []:
            self._state.threads[t.id] = t
        self._state.messages = list(messages or [])
        self._state.acks = list(acks or [])
        self._pending = _PendingWrites()
        self._events: list[DomainEvent] = []
        self._now = now or datetime.now(UTC)

    @property
    def pr_id(self) -> uuid.UUID:
        return self._state.pr_id

    @property
    def org_id(self) -> uuid.UUID:
        return self._state.org_id

    @property
    def reviews(self) -> list[Review]:
        return sorted(self._state.reviews.values(), key=lambda r: r.sequence_number)

    @property
    def findings(self) -> list[Finding]:
        return list(self._state.findings.values())

    @property
    def threads(self) -> list[CommentThread]:
        return list(self._state.threads.values())

    @property
    def messages(self) -> list[CommentMessage]:
        return list(self._state.messages)

    @property
    def events(self) -> list[DomainEvent]:
        return list(self._events)

    @property
    def pending(self) -> _PendingWrites:
        return self._pending

    def pop_events(self) -> list[DomainEvent]:
        out = list(self._events)
        self._events.clear()
        return out

    def pop_pending(self) -> _PendingWrites:
        out = self._pending
        self._pending = _PendingWrites()
        return out

    # ─── Reviews ────────────────────────────────────────────────────────────

    def start_review(
        self,
        *,
        trigger: ReviewTrigger,
        scope: ReviewScope,
        commit_sha: str,
        review_id: uuid.UUID | None = None,
    ) -> Review:
        review_id = review_id or uuid.uuid4()
        sequence_number = max((r.sequence_number for r in self._state.reviews.values()), default=0) + 1
        review = Review(
            id=review_id,
            pr_id=self._state.pr_id,
            org_id=self._state.org_id,
            sequence_number=sequence_number,
            trigger_reason=trigger,
            scope=scope,
            commit_sha_at_start=commit_sha,
            status="queued",
            superseded_by_review_id=None,
            pending_replay=False,
            created_at=self._now,
        )
        self._state.reviews[review_id] = review
        self._pending.new_reviews.append(review)
        self._events.append(
            ReviewRequested(review_id=review.id, pr_id=self._state.pr_id, trigger=trigger, scope=scope)
        )
        return review

    def mark_review_running(self, review_id: uuid.UUID, commit_sha: str) -> None:
        review = self._state.reviews[review_id]
        review.status = "running"
        review.commit_sha_at_start = commit_sha
        self._pending.updated_reviews.append(review)
        self._events.append(
            ReviewStarted(review_id=review.id, pr_id=self._state.pr_id, commit_sha=commit_sha)
        )

    def complete_review(self, review_id: uuid.UUID, findings_observed: list[uuid.UUID]) -> None:
        review = self._state.reviews[review_id]
        review.status = "done"
        self._pending.updated_reviews.append(review)
        self._events.append(
            ReviewCompleted(review_id=review.id, pr_id=self._state.pr_id, findings_observed=findings_observed)
        )

    def fail_review(self, review_id: uuid.UUID, reason: str) -> None:
        review = self._state.reviews[review_id]
        review.status = "failed"
        self._pending.updated_reviews.append(review)
        self._events.append(ReviewFailed(review_id=review.id, pr_id=self._state.pr_id, reason=reason))

    def supersede_review(self, review_id: uuid.UUID, by_review_id: uuid.UUID) -> None:
        review = self._state.reviews[review_id]
        review.status = "superseded"
        review.superseded_by_review_id = by_review_id
        self._pending.updated_reviews.append(review)
        self._events.append(
            ReviewSuperseded(review_id=review.id, pr_id=self._state.pr_id, by_review_id=by_review_id)
        )

    def set_pending_replay(self, review_id: uuid.UUID, value: bool = True) -> None:
        review = self._state.reviews[review_id]
        review.pending_replay = value
        self._pending.updated_reviews.append(review)

    def in_flight_review(self) -> Review | None:
        for r in self._state.reviews.values():
            if r.status in {"queued", "running"}:
                return r
        return None

    # ─── Finding admission / post-processing ────────────────────────────────

    def post_process_raw_findings(
        self,
        review_id: uuid.UUID,
        raw: list[RawFinding],
        *,
        diff_files: set[str] | None = None,
    ) -> tuple[list[Finding], list[FindingObservation], list[AdmissionDrop]]:
        """Apply schema → threshold → off-diff → nit cap → cross-file dedup → top-10 cap → dedup vs prior.

        Returns the survivors actually written (new + re-observed) plus a list
        of audit drops.

        `diff_files`: when supplied, findings whose anchor file isn't in the
        set get dropped with reason `off_diff`. The full-review caller
        doesn't pass it, so the suppression is opt-in.
        """
        drops: list[AdmissionDrop] = []

        # 1. Schema gate + off-diff drop.
        kept_a = []
        for rf in raw:
            if len(rf.concrete_failure_scenario.strip()) < _MIN_SCENARIO_LEN:
                drops.append(
                    AdmissionDrop(
                        rule_id=rf.rule_id,
                        reason="malformed",
                        severity=rf.severity,
                        confidence=rf.confidence,
                    )
                )
                continue
            if diff_files is not None and rf.anchor.file_path not in diff_files:
                drops.append(
                    AdmissionDrop(
                        rule_id=rf.rule_id,
                        reason="off_diff",
                        severity=rf.severity,
                        confidence=rf.confidence,
                    )
                )
                continue
            threshold = _POST_THRESHOLD[rf.severity]
            if rf.confidence < threshold:
                drops.append(
                    AdmissionDrop(
                        rule_id=rf.rule_id,
                        reason="below_threshold",
                        severity=rf.severity,
                        confidence=rf.confidence,
                    )
                )
                continue
            kept_a.append(rf)

        # 2. Per-PR nit cap. Count nits ever posted for this PR
        # (all open or terminal findings count — once we post, we've used a slot).
        nits_already = sum(1 for f in self._state.findings.values() if f.severity == "nit")
        kept_b: list[RawFinding] = []
        nit_budget = max(0, _PER_PR_NIT_CAP - nits_already)
        for rf in kept_a:
            if rf.severity != "nit":
                kept_b.append(rf)
                continue
            if nit_budget > 0:
                kept_b.append(rf)
                nit_budget -= 1
            else:
                drops.append(
                    AdmissionDrop(
                        rule_id=rf.rule_id,
                        reason="nit_cap",
                        severity=rf.severity,
                        confidence=rf.confidence,
                    )
                )

        # 3. Split into matches-existing vs candidates-for-new.
        # Matches against acknowledged findings drop silently.
        # Matches against open findings → re-observation.
        existing_by_fp: dict[str, Finding] = {f.fingerprint.hash: f for f in self._state.findings.values()}
        re_observed: list[tuple[RawFinding, Finding]] = []
        candidates_new: list[RawFinding] = []
        for rf in kept_b:
            existing = existing_by_fp.get(rf.fingerprint.hash)
            if existing is None:
                candidates_new.append(rf)
                continue
            if existing.state == FindingState.ACKNOWLEDGED:
                drops.append(
                    AdmissionDrop(
                        rule_id=rf.rule_id,
                        reason="matches_ack",
                        severity=rf.severity,
                        confidence=rf.confidence,
                    )
                )
                continue
            # Open or terminal-but-resolved findings: record re-observation
            # only when still open. Terminal resolved/stale are skipped silently.
            if existing.state == FindingState.OPEN:
                re_observed.append((rf, existing))

        # 4. Cross-file dedup. Merge `duplicate_of_rule_ids` on
        # candidates_new. Survivor's body gets a "Also in: …" footer listing
        # the duplicated file paths so the developer sees the full scope in
        # one comment instead of N.
        merged_new: dict[str, RawFinding] = {}
        merged_dup_files: dict[str, list[str]] = {}
        for rf in candidates_new:
            key = rf.fingerprint.hash
            if key in merged_new:
                continue  # exact dup; skip
            # If `rf` is a duplicate-of one already in merged_new (or vice
            # versa), don't add — annotate the existing entry with rf's file.
            absorbed = False
            for existing_key, other in list(merged_new.items()):
                if rf.rule_id in other.duplicate_of_rule_ids or other.rule_id in rf.duplicate_of_rule_ids:
                    merged_dup_files.setdefault(existing_key, []).append(rf.anchor.file_path)
                    absorbed = True
                    break
            if absorbed:
                continue
            merged_new[key] = rf
        # Apply the file-list footer to survivors that absorbed dups.
        for key, dup_files in merged_dup_files.items():
            rf = merged_new[key]
            files = [rf.anchor.file_path, *dup_files]
            footer = "\n\nAlso in: " + ", ".join(files)
            # Rebuild as a new RawFinding (frozen-ish dataclass — just dataclass.replace).
            from dataclasses import replace as _dc_replace  # noqa: PLC0415

            merged_new[key] = _dc_replace(rf, body=rf.body + footer)

        # 5. Per-review top-10 cap — applied to candidates_new only.
        ranked = sorted(
            merged_new.values(),
            key=lambda r: _SEVERITY_WEIGHT[r.severity] * r.confidence,
            reverse=True,
        )
        winners = ranked[:_PER_REVIEW_TOP_CAP]
        for rf in ranked[_PER_REVIEW_TOP_CAP:]:
            drops.append(
                AdmissionDrop(
                    rule_id=rf.rule_id,
                    reason="top_cap",
                    severity=rf.severity,
                    confidence=rf.confidence,
                )
            )

        # 6. Materialize.
        new_findings: list[Finding] = []
        new_observations: list[FindingObservation] = []
        for rf in winners:
            f = self._raise_finding(review_id, rf)
            new_findings.append(f)
            new_observations.append(self._record_observation(f, review_id, rf))
        for rf, existing in re_observed:
            self._re_observe(existing, review_id, rf)
            new_observations.append(self._record_observation(existing, review_id, rf))

        return new_findings, new_observations, drops

    def _raise_finding(self, review_id: uuid.UUID, rf: RawFinding) -> Finding:
        f = Finding(
            id=uuid.uuid4(),
            pr_id=self._state.pr_id,
            org_id=self._state.org_id,
            fingerprint=rf.fingerprint,
            rule_id=rf.rule_id,
            title=rf.title,
            body=rf.body,
            rationale=rf.rationale,
            concrete_failure_scenario=rf.concrete_failure_scenario,
            confidence=rf.confidence,
            severity=rf.severity,
            state=FindingState.OPEN,
            current_anchor=rf.anchor,
            source_agent=rf.source_agent,
            first_seen_review_id=review_id,
            last_observed_review_id=review_id,
            created_at=self._now,
            updated_at=self._now,
        )
        self._state.findings[f.id] = f
        self._pending.new_findings.append(f)
        self._events.append(FindingRaised(finding_id=f.id, pr_id=self._state.pr_id))
        return f

    def _re_observe(self, existing: Finding, review_id: uuid.UUID, rf: RawFinding) -> None:
        # Severity is sticky; confidence is max(stored, new).
        existing.confidence = max(existing.confidence, rf.confidence)
        existing.last_observed_review_id = review_id
        existing.current_anchor = rf.anchor
        existing.updated_at = self._now
        self._pending.updated_findings.append(existing)
        self._events.append(FindingReObserved(finding_id=existing.id, review_id=review_id))

    def _record_observation(
        self, finding: Finding, review_id: uuid.UUID, rf: RawFinding
    ) -> FindingObservation:
        obs = FindingObservation(
            id=uuid.uuid4(),
            finding_id=finding.id,
            review_id=review_id,
            anchor=rf.anchor,
            raw_body=rf.body,
            created_at=self._now,
        )
        self._state.observations.append(obs)
        self._pending.new_observations.append(obs)
        return obs

    # ─── Threads + messages ─────────────────────────────────────────────────

    def open_thread_for_finding(
        self,
        finding_id: uuid.UUID,
        *,
        external_thread_id: str | None = None,
        thread_id: uuid.UUID | None = None,
    ) -> CommentThread:
        # Idempotent — return existing thread if one already exists.
        for t in self._state.threads.values():
            if t.finding_id == finding_id:
                if external_thread_id and not t.external_thread_id:
                    t.external_thread_id = external_thread_id
                return t
        thread = CommentThread(
            id=thread_id or uuid.uuid4(),
            finding_id=finding_id,
            external_thread_id=external_thread_id,
            created_at=self._now,
            updated_at=self._now,
        )
        self._state.threads[thread.id] = thread
        self._pending.new_threads.append(thread)
        return thread

    def thread_for_finding(self, finding_id: uuid.UUID) -> CommentThread | None:
        for t in self._state.threads.values():
            if t.finding_id == finding_id:
                return t
        return None

    def append_message(
        self,
        *,
        thread_id: uuid.UUID,
        author_kind: AuthorKind,
        author_external_id: str,
        external_comment_id: str,
        body: str,
        in_reply_to_external_id: str | None = None,
        classified_intent: ReplyIntent | None = None,
    ) -> CommentMessage:
        msg = CommentMessage(
            id=uuid.uuid4(),
            thread_id=thread_id,
            author_kind=author_kind,
            author_external_id=author_external_id,
            external_comment_id=external_comment_id,
            in_reply_to_external_id=in_reply_to_external_id,
            body=body,
            classified_intent=classified_intent,
            created_at=self._now,
        )
        self._state.messages.append(msg)
        self._pending.new_messages.append(msg)
        if author_kind == "yaaos":
            self._events.append(AgentReplyPosted(thread_id=thread_id, message_id=msg.id))
        elif classified_intent is not None:
            self._events.append(
                CommentReplyReceived(
                    thread_id=thread_id,
                    message_id=msg.id,
                    classified_intent=classified_intent,
                )
            )
        return msg

    # ─── State transitions ──────────────────────────────────────────────────

    def acknowledge(
        self,
        *,
        finding_id: uuid.UUID,
        kind: AckKind,
        rationale: str,
        made_by_external_id: str,
        made_by_message_id: uuid.UUID,
    ) -> AcknowledgmentDecision:
        finding = self._state.findings[finding_id]
        new_state = state_machine.transition(finding.state, FindingState.ACKNOWLEDGED)
        prev = finding.state
        finding.state = new_state
        finding.updated_at = self._now
        self._pending.updated_findings.append(finding)
        ack = AcknowledgmentDecision(
            id=uuid.uuid4(),
            finding_id=finding_id,
            kind=kind,
            rationale=rationale,
            made_by_external_id=made_by_external_id,
            made_by_message_id=made_by_message_id,
            created_at=self._now,
        )
        self._state.acks.append(ack)
        self._pending.new_acks.append(ack)
        self._events.append(FindingStateChanged(finding_id=finding.id, from_state=prev, to_state=new_state))
        self._events.append(FindingAcknowledged(finding_id=finding.id, ack_id=ack.id, kind=kind))
        return ack

    def record_fix_verification(
        self, *, finding_id: uuid.UUID, still_present: bool, confidence: float, threshold: float = 0.80
    ) -> FindingState | None:
        """Transition based on `verify_fix` result.

        Returns the new state if we transitioned, else None. The aggregate
        does NOT post the reply — `service.py` does that based on the event +
        the confidence rubric.
        """
        if confidence < threshold:
            return None
        finding = self._state.findings[finding_id]
        if still_present:
            return None  # stays open
        new_state = state_machine.transition(finding.state, FindingState.RESOLVED_CONFIRMED)
        prev = finding.state
        finding.state = new_state
        finding.updated_at = self._now
        self._pending.updated_findings.append(finding)
        self._events.append(FindingStateChanged(finding_id=finding.id, from_state=prev, to_state=new_state))
        self._events.append(
            FindingResolutionDetected(finding_id=finding.id, kind=FindingState.RESOLVED_CONFIRMED)
        )
        return new_state

    def record_stale_detection(
        self,
        *,
        finding_id: uuid.UUID,
        still_applies: bool,
        confidence: float,
        threshold: float = 0.80,
    ) -> FindingState | None:
        if confidence < threshold:
            return None
        finding = self._state.findings[finding_id]
        if still_applies:
            return None
        new_state = state_machine.transition(finding.state, FindingState.STALE)
        prev = finding.state
        finding.state = new_state
        finding.updated_at = self._now
        self._pending.updated_findings.append(finding)
        self._events.append(FindingStateChanged(finding_id=finding.id, from_state=prev, to_state=new_state))
        self._events.append(FindingStaleDetected(finding_id=finding.id))
        return new_state

    def mark_unverified_resolution(self, finding_id: uuid.UUID) -> FindingState:
        """Anchor gone in the new commit and no verify-fix possible."""
        finding = self._state.findings[finding_id]
        new_state = state_machine.transition(finding.state, FindingState.RESOLVED_UNVERIFIED)
        prev = finding.state
        finding.state = new_state
        finding.updated_at = self._now
        self._pending.updated_findings.append(finding)
        self._events.append(FindingStateChanged(finding_id=finding.id, from_state=prev, to_state=new_state))
        self._events.append(
            FindingResolutionDetected(finding_id=finding.id, kind=FindingState.RESOLVED_UNVERIFIED)
        )
        return new_state

    def update_anchor(self, finding_id: uuid.UUID, new_anchor: CodeAnchor) -> None:
        finding = self._state.findings[finding_id]
        finding.current_anchor = new_anchor
        finding.updated_at = self._now
        self._pending.updated_findings.append(finding)
        self._events.append(FindingAnchorUpdated(finding_id=finding.id, new_anchor=new_anchor))

    # ─── Queries ────────────────────────────────────────────────────────────

    def open_findings_in_files(self, file_paths: set[str]) -> list[Finding]:
        """All open findings whose anchor file is in `file_paths`."""
        return [
            f
            for f in self._state.findings.values()
            if f.state == FindingState.OPEN and f.current_anchor.file_path in file_paths
        ]

    def latest_review(self) -> Review | None:
        if not self._state.reviews:
            return None
        return max(self._state.reviews.values(), key=lambda r: r.sequence_number)
