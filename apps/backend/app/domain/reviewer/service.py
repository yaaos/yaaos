"""Service layer for the durable-findings flows (plan §6.4, §6.5, §9).

Pure orchestration over the aggregate. The DB session + VCS adapter calls
are pulled in at the route layer (see `web.py`); the helpers here take the
already-loaded aggregate so they're unit-testable without a database.

POC layout:

- `apply_classified_reply` — given a classifier output, transition the
  aggregate (acknowledge / no-op for mid-band) and return the yaaos reply
  body the caller should post.
- `apply_verify_fix_result` — given a coding-agent verify_fix result,
  transition the aggregate per plan §10.4 and return the reply body.
- `apply_stale_check_result` — given a coding-agent stale_check result,
  transition the aggregate per plan §10.4 and return the reply body.
- `is_yaaos_command` / `is_off_topic_message` — cheap deterministic checks
  applied before the classifier per plan §6.4 step 2.
- `pr_review_view` — read model bundling the aggregate's data into the shape
  the UI consumes (multi-review timeline, All Conversations cross-cut).

Confidence thresholds match plan §10.3 / §10.4 and are class constants here
so the tests pin them too.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.llm import ClassifyReplyOutput
from app.domain.reviewer.types import (
    AckKind,
    CommentMessage,
    Finding,
    FindingState,
    Review,
)

# Plan §10.3 — classification confidence bands.
CLASSIFY_ACT_THRESHOLD = 0.85
CLASSIFY_CONFIRM_THRESHOLD = 0.60

# Plan §10.4 — verify_fix / stale_check.
VERIFY_ACT_THRESHOLD = 0.80
VERIFY_OBSERVE_THRESHOLD = 0.50

_YAAOS_COMMAND_RE = re.compile(r"@yaaos\s+(review|full\s+review|cancel)\b", re.IGNORECASE)
_FIX_CLAIM_RE = re.compile(r"\b(fix(ed|ing)?|done|address(ed|ing)?|resolved)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ReplyAction:
    """What the caller should do after the aggregate mutation.

    `kind`:
      - `acknowledge_posted` — finding moved to acknowledged; post `reply_body`.
      - `confirm_requested` — mid-band acknowledgment; post `reply_body` asking
        the developer to type `confirm`.
      - `verify_fix_triggered` — kick off the verify-fix subflow; `reply_body`
        is None (subflow posts its own reply when it completes).
      - `noop` — store the developer message, no transition, no reply.
    """

    kind: Literal[
        "acknowledge_posted",
        "confirm_requested",
        "verify_fix_triggered",
        "noop",
    ]
    reply_body: str | None = None


@dataclass(frozen=True)
class VerifyFixAction:
    kind: Literal["resolved", "still_present_observed", "low_confidence_noop"]
    reply_body: str


@dataclass(frozen=True)
class StaleCheckAction:
    kind: Literal["stale_marked", "still_applies_observed", "low_confidence_noop"]
    reply_body: str


# ─── Deterministic pre-classifier checks (plan §6.4 step 2) ────────────────


def is_yaaos_command(body: str) -> str | None:
    """Returns the command name (`review` | `full review` | `cancel`) or None."""
    m = _YAAOS_COMMAND_RE.search(body)
    return m.group(1).lower().replace("  ", " ") if m else None


def is_off_topic_message(body: str) -> bool:
    """Heuristic: short, no question, no fix claim → don't classify (plan §6.4 step 2)."""
    stripped = body.strip()
    if "?" in stripped:
        return False
    if _FIX_CLAIM_RE.search(stripped):
        return False
    return len(stripped.split()) < 5


# ─── Classifier → aggregate transition ─────────────────────────────────────


def apply_classified_reply(
    aggregate: PRReviewAggregate,
    *,
    finding_id: uuid.UUID,
    classification: ClassifyReplyOutput,
    reply_message: CommentMessage,
) -> ReplyAction:
    """Apply the classifier output to the aggregate.

    `reply_message` is the developer's just-stored message (already appended
    to the thread by the caller). The aggregate mutates state; the returned
    `ReplyAction` tells the caller what reply (if any) to post via VCS.
    """
    intent = classification.intent
    confidence = classification.confidence

    if intent == "acknowledgment":
        if confidence >= CLASSIFY_ACT_THRESHOLD:
            kind: AckKind = classification.suggested_ack_kind or "intentional"
            aggregate.acknowledge(
                finding_id=finding_id,
                kind=kind,
                rationale=reply_message.body,
                made_by_external_id=reply_message.author_external_id,
                made_by_message_id=reply_message.id,
            )
            return ReplyAction(
                kind="acknowledge_posted",
                reply_body="Noted — I'll skip this in future reviews.",
            )
        if confidence >= CLASSIFY_CONFIRM_THRESHOLD:
            return ReplyAction(
                kind="confirm_requested",
                reply_body=(
                    "Reading this as 'intentional / wontfix' — reply `confirm` to acknowledge, "
                    "otherwise I'll treat it as a question."
                ),
            )
        return ReplyAction(kind="noop")

    if intent == "verify_fix":
        if confidence >= CLASSIFY_ACT_THRESHOLD:
            return ReplyAction(kind="verify_fix_triggered")
        return ReplyAction(kind="noop")

    # `other` — store message, no transition.
    return ReplyAction(kind="noop")


# ─── verify_fix → aggregate transition (plan §6.5 + §10.4) ─────────────────


def apply_verify_fix_result(
    aggregate: PRReviewAggregate,
    *,
    finding_id: uuid.UUID,
    still_present: bool,
    confidence: float,
    observed_line: int | None = None,
) -> VerifyFixAction:
    """Decide what to do with a coding_agent.verify_fix result.

    - ≥ 0.80, not present → mark resolved, post "confirmed fixed" reply.
    - ≥ 0.80, still present → post "still see the issue" reply; stay open.
    - 0.50-0.79 → post "unclear if fixed" reply; stay open.
    - < 0.50 → no reply; stay open.
    """
    if confidence < VERIFY_OBSERVE_THRESHOLD:
        return VerifyFixAction(kind="low_confidence_noop", reply_body="")

    if confidence >= VERIFY_ACT_THRESHOLD:
        if still_present:
            line_note = f" at line {observed_line}" if observed_line is not None else ""
            return VerifyFixAction(
                kind="still_present_observed",
                reply_body=f"I still see the issue{line_note}.",
            )
        aggregate.record_fix_verification(
            finding_id=finding_id,
            still_present=False,
            confidence=confidence,
            threshold=VERIFY_ACT_THRESHOLD,
        )
        return VerifyFixAction(
            kind="resolved",
            reply_body="Confirmed fixed.",
        )

    # 0.50-0.79: observe but don't transition.
    return VerifyFixAction(
        kind="still_present_observed" if still_present else "low_confidence_noop",
        reply_body="Unclear if this is fixed — could you point me at the change?",
    )


# ─── stale_check → aggregate transition (plan §6.2 + §10.4) ────────────────


def apply_stale_check_result(
    aggregate: PRReviewAggregate,
    *,
    finding_id: uuid.UUID,
    still_applies: bool,
    confidence: float,
) -> StaleCheckAction:
    if confidence < VERIFY_OBSERVE_THRESHOLD:
        return StaleCheckAction(kind="low_confidence_noop", reply_body="")

    if confidence >= VERIFY_ACT_THRESHOLD:
        if not still_applies:
            aggregate.record_stale_detection(
                finding_id=finding_id,
                still_applies=False,
                confidence=confidence,
                threshold=VERIFY_ACT_THRESHOLD,
            )
            return StaleCheckAction(
                kind="stale_marked",
                reply_body="This no longer applies after the latest changes — marking stale.",
            )
        return StaleCheckAction(
            kind="still_applies_observed",
            reply_body="",
        )

    return StaleCheckAction(
        kind="low_confidence_noop",
        reply_body="",
    )


# ─── Read views (plan §9) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class FindingView:
    """Read-model row consumed by the multi-review UI (plan §9.2)."""

    id: uuid.UUID
    state: FindingState
    severity: str
    rule_id: str
    title: str
    body: str
    rationale: str
    confidence: int
    first_seen_review_id: uuid.UUID
    last_observed_review_id: uuid.UUID
    file_path: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class ConversationView:
    """One entry in the All Conversations cross-cut (plan §9.3)."""

    finding_id: uuid.UUID
    state: FindingState
    severity: str
    title: str
    first_seen_review_id: uuid.UUID
    last_message_preview: str
    reply_count: int


def list_findings_view(aggregate: PRReviewAggregate, *, include_terminal: bool = False) -> list[FindingView]:
    out: list[FindingView] = []
    for f in aggregate.findings:
        if not include_terminal and f.state.is_terminal and f.state != FindingState.ACKNOWLEDGED:
            continue
        out.append(_finding_view(f))
    return out


def _finding_view(f: Finding) -> FindingView:
    return FindingView(
        id=f.id,
        state=f.state,
        severity=f.severity,
        rule_id=f.rule_id,
        title=f.title,
        body=f.body,
        rationale=f.rationale,
        confidence=f.confidence,
        first_seen_review_id=f.first_seen_review_id,
        last_observed_review_id=f.last_observed_review_id,
        file_path=f.current_anchor.file_path,
        line_start=f.current_anchor.line_start,
        line_end=f.current_anchor.line_end,
    )


def all_conversations_view(aggregate: PRReviewAggregate) -> list[ConversationView]:
    """Plan §9.3 — findings with ≥1 dev reply OR open + first-raised before the latest review.

    Excludes resolved_confirmed / resolved_unverified / stale (POC, plan §15).
    """
    latest = aggregate.latest_review()
    latest_seq = latest.sequence_number if latest else None
    out: list[ConversationView] = []
    for thread in aggregate.threads:
        finding = next((f for f in aggregate.findings if f.id == thread.finding_id), None)
        if finding is None:
            continue
        if finding.state in {
            FindingState.RESOLVED_CONFIRMED,
            FindingState.RESOLVED_UNVERIFIED,
            FindingState.STALE,
        }:
            continue
        msgs = [m for m in _messages_for_thread(aggregate, thread.id)]
        human_replies = [m for m in msgs if m.author_kind == "human"]
        if not human_replies:
            if finding.state != FindingState.OPEN:
                continue
            if latest_seq is None:
                continue
            # First-raised before the latest review — surface as a buried conversation.
            first_seen_review = next(
                (r for r in aggregate.reviews if r.id == finding.first_seen_review_id), None
            )
            if first_seen_review is None or first_seen_review.sequence_number >= latest_seq:
                continue
        last_message_preview = (msgs[-1].body if msgs else finding.title)[:120]
        out.append(
            ConversationView(
                finding_id=finding.id,
                state=finding.state,
                severity=finding.severity,
                title=finding.title,
                first_seen_review_id=finding.first_seen_review_id,
                last_message_preview=last_message_preview,
                reply_count=len(human_replies),
            )
        )
    return out


def _messages_for_thread(aggregate: PRReviewAggregate, thread_id: uuid.UUID) -> list[CommentMessage]:
    return [m for m in aggregate.messages if m.thread_id == thread_id]


def review_summary(aggregate: PRReviewAggregate, review: Review) -> dict[str, int]:
    """Counters for the per-review section header (plan §9.2): N new, M re-observed, K resolved."""
    new = 0
    re_observed = 0
    resolved = 0
    for f in aggregate.findings:
        if f.first_seen_review_id == review.id:
            new += 1
        if f.last_observed_review_id == review.id and f.first_seen_review_id != review.id:
            re_observed += 1
    for f in aggregate.findings:
        # Resolved-by-this-review can be derived later from FindingStateChanged
        # events bound to this review; for POC we just expose new + re_observed.
        del f
    return {
        "new": new,
        "re_observed": re_observed,
        "resolved": resolved,
    }
