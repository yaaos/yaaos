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
from typing import Any, Literal

from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.llm import ClassifyReplyOutput
from app.domain.reviewer.types import (
    AckKind,
    CommentMessage,
    Finding,
    FindingState,
    Review,
)

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
      - `answer_question_triggered` — kick off the answer-question subflow;
        `reply_body` is None (subflow posts its own reply when it completes).
      - `noop` — store the developer message, no transition, no reply.
    """

    kind: Literal[
        "acknowledge_posted",
        "confirm_requested",
        "verify_fix_triggered",
        "answer_question_triggered",
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

    The classifier emits one of five categorical intents (see
    `domain/reviewer/llm/classifier.py`) that each map 1:1 onto a
    `ReplyAction.kind`. No probability thresholds — the label IS the action.
    """
    intent = classification.intent

    if intent == "acknowledgment_clear":
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

    if intent == "acknowledgment_unclear":
        return ReplyAction(
            kind="confirm_requested",
            reply_body=(
                "Reading this as 'intentional / wontfix' — reply `confirm` to acknowledge, "
                "otherwise I'll treat it as a question."
            ),
        )

    if intent == "verify_fix":
        return ReplyAction(kind="verify_fix_triggered")

    if intent == "question":
        return ReplyAction(kind="answer_question_triggered")

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
    """Plan §9.3 — findings with at least one developer reply.

    A "conversation" in this view means an actual back-and-forth: the
    finding's thread has ≥1 `author_kind='human'` message. Findings yaaos
    raised but the developer never responded to don't count — the per-
    review timeline already surfaces them.

    Terminal-state findings (resolved_confirmed / resolved_unverified /
    stale) are excluded.
    """
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
        msgs = list(_messages_for_thread(aggregate, thread.id))
        human_replies = [m for m in msgs if m.author_kind == "human"]
        if not human_replies:
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


# ─── Plan §5.1 public Python API ───────────────────────────────────────────


async def list_reviews_for_pr(pr_id: uuid.UUID, *, org_id: uuid.UUID) -> list[Review]:
    """List Review entities for a PR, newest first by sequence_number."""
    from sqlalchemy import desc, select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415
    from app.domain.reviewer.repository import _review_from_row  # noqa: PLC0415

    async with db_session() as s:
        rows = (
            (
                await s.execute(
                    select(ReviewRow)
                    .where(ReviewRow.pr_id == pr_id, ReviewRow.org_id == org_id)
                    .order_by(desc(ReviewRow.sequence_number))
                )
            )
            .scalars()
            .all()
        )
    return [_review_from_row(r) for r in rows]


async def get_review(review_id: uuid.UUID, *, org_id: uuid.UUID) -> Review:
    """Fetch one Review by id. Raises `LookupError` if missing."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import ReviewRow  # noqa: PLC0415
    from app.domain.reviewer.repository import _review_from_row  # noqa: PLC0415

    async with db_session() as s:
        row = (
            await s.execute(select(ReviewRow).where(ReviewRow.id == review_id, ReviewRow.org_id == org_id))
        ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"review {review_id} not found in org {org_id}")
    return _review_from_row(row)


async def list_findings_for_pr(
    pr_id: uuid.UUID, *, org_id: uuid.UUID, include_terminal: bool = False
) -> list[FindingView]:
    """Plan §5.1: list findings for a PR. Default excludes resolved+stale."""
    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.repository import SqlAlchemyAggregateRepository  # noqa: PLC0415

    async with db_session() as s:
        repo = SqlAlchemyAggregateRepository(s)
        agg = await repo.load(pr_id=pr_id, org_id=org_id)
    return list_findings_view(agg, include_terminal=include_terminal)


async def get_thread(thread_id: uuid.UUID, *, org_id: uuid.UUID) -> ThreadView | None:
    """Plan §5.1: fetch a thread view (messages + ack) by thread id.

    Returns None when the thread doesn't exist or belongs to a different org
    (the FindingRow row's org_id is the source of truth here).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import (  # noqa: PLC0415
        AcknowledgmentDecisionRow,
        CommentMessageRow,
        CommentThreadRow,
        FindingRow,
    )

    async with db_session() as s:
        row = (
            await s.execute(
                select(CommentThreadRow, FindingRow)
                .join(FindingRow, FindingRow.id == CommentThreadRow.finding_id)
                .where(CommentThreadRow.id == thread_id, FindingRow.org_id == org_id)
            )
        ).first()
        if row is None:
            return None
        thread_row, finding_row = row
        messages = list(
            (
                await s.execute(
                    select(CommentMessageRow)
                    .where(CommentMessageRow.thread_id == thread_id)
                    .order_by(CommentMessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        ack = (
            await s.execute(
                select(AcknowledgmentDecisionRow)
                .where(AcknowledgmentDecisionRow.finding_id == finding_row.id)
                .order_by(AcknowledgmentDecisionRow.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    return ThreadView(
        thread_id=thread_row.id,
        finding_id=finding_row.id,
        external_thread_id=thread_row.external_thread_id,
        messages=[
            ThreadMessageView(
                id=m.id,
                author_kind=m.author_kind,
                author_external_id=m.author_external_id,
                external_comment_id=m.external_comment_id,
                body=m.body,
                classified_intent=m.classified_intent,
            )
            for m in messages
        ],
        ack_kind=ack.kind if ack else None,
        ack_rationale=ack.rationale if ack else None,
    )


@dataclass(frozen=True)
class ThreadMessageView:
    id: uuid.UUID
    author_kind: str
    author_external_id: str
    external_comment_id: str
    body: str
    classified_intent: str | None


@dataclass(frozen=True)
class ThreadView:
    thread_id: uuid.UUID
    finding_id: uuid.UUID
    external_thread_id: str | None
    messages: list[ThreadMessageView]
    ack_kind: str | None
    ack_rationale: str | None


# ─── Plan §10.13 eval metrics ──────────────────────────────────────────────


async def compute_acceptance_rate(*, org_id: uuid.UUID) -> float:
    """Plan §10.13: (findings that led to a developer code change) / (findings posted).

    A finding "led to a code change" iff its current state is
    `resolved_confirmed` (agent verified the fix) OR `resolved_unverified`
    (anchor gone — block was deleted or rewritten). Both indicate the
    developer touched the flagged code. `acknowledged` (wontfix/intentional)
    and `stale` (no longer applies) do NOT count: no code change there.

    Returns 0.0 when no findings exist.
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415

    async with db_session() as s:
        total = (
            await s.execute(select(func.count(FindingRow.id)).where(FindingRow.org_id == org_id))
        ).scalar_one()
        accepted = (
            await s.execute(
                select(func.count(FindingRow.id)).where(
                    FindingRow.org_id == org_id,
                    FindingRow.state.in_(["resolved_confirmed", "resolved_unverified"]),
                )
            )
        ).scalar_one()
    return float(accepted) / float(total) if total else 0.0


async def compute_resolved_without_edit_rate(*, org_id: uuid.UUID) -> float:
    """Plan §10.13: (resolved-without-edit) / (findings posted). Higher = more noise.

    Proxy today: `acknowledged` (wontfix or intentional) + `resolved_unverified`
    + `stale` count as "marked resolved without edit". Returns 0.0 when empty.
    """
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415

    async with db_session() as s:
        total = (
            await s.execute(select(func.count(FindingRow.id)).where(FindingRow.org_id == org_id))
        ).scalar_one()
        without_edit = (
            await s.execute(
                select(func.count(FindingRow.id)).where(
                    FindingRow.org_id == org_id,
                    FindingRow.state.in_(["acknowledged", "resolved_unverified", "stale"]),
                )
            )
        ).scalar_one()
    return float(without_edit) / float(total) if total else 0.0


# ─── Domain events dispatch (plan §5.2) ────────────────────────────────────


async def dispatch_events(aggregate: PRReviewAggregate) -> list[Any]:
    """Drain the aggregate's pending events to `core/events` subscribers.

    Returns the list of events that were dispatched (mostly for tests / audit).
    Service-layer callers invoke this after `repo.save(aggregate)` so the
    in-process event bus sees domain transitions.
    """
    from app.core.events import publish  # noqa: PLC0415

    events = aggregate.pop_events()
    for event in events:
        # Wrap the dataclass event in a Pydantic core/events `Event` shape on
        # the fly so the in-process bus can dispatch by `kind`. The dispatch
        # path turns the dataclass into a generic dict-carrier event.
        await publish(_DomainEventEnvelope.wrap(event))
    return events


# Domain events that represent durable-finding state transitions worth an
# audit row (plan §5.3). Reviews already audit via `audit_for_review_job`.
_AUDIT_FINDING_EVENT_KINDS = {
    "FindingRaised": "finding_raised",
    "FindingReObserved": "finding_re_observed",
    "FindingAnchorUpdated": "finding_anchor_updated",
    "FindingStateChanged": "finding_state_changed",
    "FindingAcknowledged": "finding_acknowledged",
    "FindingResolutionDetected": "finding_resolution_detected",
    "FindingStaleDetected": "finding_stale_detected",
}


async def dispatch_audits(
    aggregate: PRReviewAggregate,
    *,
    session: Any,
    actor: Any,
    org_id: uuid.UUID,
) -> int:
    """Plan §5.3: write an `audit_entries` row per finding state transition.

    Peeks at `aggregate.events` (does NOT drain — `dispatch_events` owns the
    drain). Idempotent across multiple calls only insofar as the caller
    invokes it once per save cycle alongside `dispatch_events`.
    """
    from app.core.audit_log import audit_for_finding  # noqa: PLC0415
    from app.domain.reviewer.service import _DomainEventEnvelope as _Env  # noqa: PLC0415

    written = 0
    for event in aggregate.events:
        cls = type(event).__name__
        kind = _AUDIT_FINDING_EVENT_KINDS.get(cls)
        if kind is None:
            continue
        finding_id = getattr(event, "finding_id", None)
        if finding_id is None:
            continue
        await audit_for_finding(
            finding_id,
            kind,
            _Env.wrap(event),
            actor=actor,
            org_id=org_id,
            session=session,
        )
        written += 1
    return written


# ─── Generic envelope so dataclass DomainEvents fit core/events.Event ──────


from pydantic import Field  # noqa: E402

from app.core.events import Event as _BusEvent  # noqa: E402


class _DomainEventEnvelope(_BusEvent):
    """Adapter: wraps a dataclass DomainEvent as a `core/events.Event`.

    `core/events` expects Pydantic models with a `kind` discriminator.
    DomainEvents are plain @dataclasses; this envelope carries the payload
    as a dict + sets `kind` from the dataclass class name.
    """

    kind: str  # type: ignore[assignment]
    source_module: Literal["reviewer"] = "reviewer"  # type: ignore[assignment]
    payload: dict = Field(default_factory=dict)

    @classmethod
    def wrap(cls, event: Any) -> _DomainEventEnvelope:
        from dataclasses import asdict  # noqa: PLC0415

        kind_map = {
            "ReviewRequested": "review_requested",
            "ReviewStarted": "review_started",
            "ReviewCompleted": "review_completed",
            "ReviewFailed": "review_failed",
            "ReviewSuperseded": "review_superseded",
            "FindingRaised": "finding_raised",
            "FindingReObserved": "finding_re_observed",
            "FindingAnchorUpdated": "finding_anchor_updated",
            "FindingStateChanged": "finding_state_changed",
            "FindingAcknowledged": "finding_acknowledged",
            "FindingResolutionDetected": "finding_resolution_detected",
            "FindingStaleDetected": "finding_stale_detected",
            "CommentReplyReceived": "comment_reply_received",
            "AgentReplyPosted": "agent_reply_posted",
        }
        kind = kind_map.get(type(event).__name__, type(event).__name__)
        # asdict serializes dataclasses recursively; UUIDs / enums survive as-is.
        return cls(kind=kind, payload=asdict(event))  # type: ignore[arg-type]


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
