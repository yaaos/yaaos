"""Service layer for the durable-findings flows.

Pure orchestration over the aggregate. The DB session + VCS adapter calls
are pulled in at the route layer (see `web.py`); the helpers here take the
already-loaded aggregate so they're unit-testable without a database.

POC layout:

- `apply_classified_reply` — given a classifier output, transition the
  aggregate (acknowledge / no-op for mid-band) and return the yaaos reply
  body the caller should post.
- `apply_verify_fix_result` — given a coding-agent verify_fix result,
  transition the aggregate and return the reply body.
- `apply_stale_check_result` — given a coding-agent stale_check result,
  transition the aggregate and return the reply body.
- `is_yaaos_command` / `is_off_topic_message` — cheap deterministic checks
  applied before the classifier.
- `pr_review_view` — read model bundling the aggregate's data into the shape
  the UI consumes (multi-review timeline, All Conversations cross-cut).

Confidence thresholds are class constants here so the tests pin them too.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import require_org_context
from app.core.sse import GeneralEventKind, publish_general_after_commit
from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.llm import ClassifyReplyOutput
from app.domain.reviewer.types import (
    AckKind,
    CommentMessage,
    Finding,
    FindingState,
    Review,
)

# verify_fix / stale_check thresholds.
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


class FindingAuditPayload(BaseModel):
    """Typed payload for finding state-transition audit rows.

    Written by `dispatch_audits` for every finding-state event; stored in
    `audit_entries.payload` as JSON.  Internal to this module — not in
    `domain/reviewer.__all__`.
    """

    kind: str
    finding_id: uuid.UUID
    fields: dict[str, Any]


# ─── Deterministic pre-classifier checks ───────────────────────────────────


def is_yaaos_command(body: str) -> str | None:
    """Returns the command name (`review` | `full review` | `cancel`) or None."""
    m = _YAAOS_COMMAND_RE.search(body)
    return m.group(1).lower().replace("  ", " ") if m else None


def is_off_topic_message(body: str) -> bool:
    """Heuristic: short, no question, no fix claim → don't classify."""
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


# ─── verify_fix → aggregate transition ─────────────────────────────────────


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


# ─── stale_check → aggregate transition ────────────────────────────────────


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


# ─── Read views ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FindingView:
    """Read-model row consumed by the multi-review UI."""

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
    """One entry in the All Conversations cross-cut."""

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
    """Findings with at least one developer reply.

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


# ─── Public Python API ──────────────────────────────────────────────────────


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
    """List findings for a PR. Default excludes resolved+stale."""
    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.repository import SqlAlchemyAggregateRepository  # noqa: PLC0415

    async with db_session() as s:
        repo = SqlAlchemyAggregateRepository(s)
        agg = await repo.load(pr_id=pr_id, org_id=org_id)
    return list_findings_view(agg, include_terminal=include_terminal)


async def get_thread(thread_id: uuid.UUID, *, org_id: uuid.UUID) -> ThreadView | None:
    """Fetch a thread view (messages + ack) by thread id.

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


# ─── Eval metrics ────────────────────────────────────────────────────────────


async def compute_acceptance_rate(*, org_id: uuid.UUID) -> float:
    """(findings that led to a developer code change) / (findings posted).

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
    """(resolved-without-edit) / (findings posted). Higher = more noise.

    Proxy: `acknowledged` (wontfix or intentional) + `resolved_unverified`
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


# ─── Domain events dispatch ────────────────────────────────────────────────

# Maps each reviewer domain-event dataclass name to its GeneralEventKind.
# All 14 reviewer event types must be present — `_kind_for` raises on unknown
# types so a missing entry surfaces at test time, not silently at runtime.
_KIND_MAP: dict[str, GeneralEventKind] = {
    "ReviewRequested": GeneralEventKind.REVIEW_REQUESTED,
    "ReviewStarted": GeneralEventKind.REVIEW_STARTED,
    "ReviewCompleted": GeneralEventKind.REVIEW_COMPLETED,
    "ReviewFailed": GeneralEventKind.REVIEW_FAILED,
    "ReviewSuperseded": GeneralEventKind.REVIEW_SUPERSEDED,
    "FindingRaised": GeneralEventKind.FINDING_RAISED,
    "FindingReObserved": GeneralEventKind.FINDING_RE_OBSERVED,
    "FindingAnchorUpdated": GeneralEventKind.FINDING_ANCHOR_UPDATED,
    "FindingStateChanged": GeneralEventKind.FINDING_STATE_CHANGED,
    "FindingAcknowledged": GeneralEventKind.FINDING_ACKNOWLEDGED,
    "FindingResolutionDetected": GeneralEventKind.FINDING_RESOLUTION_DETECTED,
    "FindingStaleDetected": GeneralEventKind.FINDING_STALE_DETECTED,
    "CommentReplyReceived": GeneralEventKind.COMMENT_REPLY_RECEIVED,
    "AgentReplyPosted": GeneralEventKind.AGENT_REPLY_POSTED,
}


def _kind_for(event: Any) -> GeneralEventKind:
    """Resolve a reviewer domain-event dataclass instance to its GeneralEventKind.

    Raises `KeyError` for unknown types — catches missing entries at test time.
    """
    return _KIND_MAP[type(event).__name__]


def _json_safe(value: Any) -> Any:
    """Recursively coerce non-JSON-serializable values to wire-safe types.

    `asdict` on reviewer domain events produces UUIDs and StrEnum values that
    `json.dumps` can't serialize natively. Coercion rules:
    - `uuid.UUID` → `str`
    - `enum.Enum` (including `StrEnum`) → `.value`
    - `tuple` → `list` (dataclasses.asdict preserves tuples in nested structures)
    - `dict` / `list` → recurse
    - primitives (`str`, `int`, `float`, `bool`, `None`) → pass through
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def dispatch_events(session: AsyncSession, *, aggregate: PRReviewAggregate) -> list[Any]:
    """Queue the aggregate's pending events for publish after the session commits.

    Uses `publish_general_after_commit` — events are stashed on the SQLAlchemy
    session and flushed to Redis only after a successful `await session.commit()`.
    Rollbacks silently discard the stash, so rolled-back transactions never emit
    phantom SPA events.

    Returns the list of events that were queued (for tests / audit).
    """
    org_id = require_org_context()
    events = aggregate.pop_events()
    for event in events:
        payload = _json_safe(asdict(event))
        publish_general_after_commit(session, org_id=org_id, kind=_kind_for(event), payload=payload)
    return events


# Domain events that represent durable-finding state transitions worth an
# audit row. Reviews already audit via `audit_for_review_job`.
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
    """Write an `audit_entries` row per finding state transition.

    Peeks at `aggregate.events` (does NOT drain — `dispatch_events` owns the
    drain). Idempotent across multiple calls only insofar as the caller
    invokes it once per save cycle alongside `dispatch_events`.
    """
    from app.core.audit_log import audit_for_finding  # noqa: PLC0415

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
            FindingAuditPayload(kind=kind, finding_id=finding_id, fields=_json_safe(asdict(event))),
            actor=actor,
            org_id=org_id,
            session=session,
        )
        written += 1
    return written


async def find_pr_id_by_external_comment_id(external_comment_id: str) -> uuid.UUID | None:
    """Return the pr_id for the finding whose thread contains a message with the given external comment id.

    Returns None when no matching comment exists.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import (  # noqa: PLC0415
        CommentMessageRow,
        CommentThreadRow,
        FindingRow,
    )

    async with db_session() as s:
        row = (
            await s.execute(
                select(FindingRow.pr_id)
                .join(CommentThreadRow, CommentThreadRow.finding_id == FindingRow.id)
                .join(CommentMessageRow, CommentMessageRow.thread_id == CommentThreadRow.id)
                .where(CommentMessageRow.external_comment_id == external_comment_id)
            )
        ).first()
    return row[0] if row is not None else None


async def aggregate_findings_by_prs(
    pr_ids: list[uuid.UUID], *, org_id: uuid.UUID
) -> dict[uuid.UUID, tuple[int, str | None]]:
    """Return finding count and max severity for each pr_id in one batch query.

    Keys are present only for pr_ids that have at least one finding.
    Value is `(count, max_severity)` where max_severity ∈ `"high" | "medium" | "low" | None`.
    """
    from sqlalchemy import case, func, select  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.domain.reviewer.models import FindingRow  # noqa: PLC0415

    if not pr_ids:
        return {}

    severity_rank = case(
        (FindingRow.severity == "high", 3),
        (FindingRow.severity == "medium", 2),
        (FindingRow.severity == "low", 1),
        else_=0,
    )
    agg_stmt = (
        select(
            FindingRow.pr_id,
            func.count(FindingRow.id),
            func.max(severity_rank),
        )
        .where(FindingRow.pr_id.in_(pr_ids), FindingRow.org_id == org_id)
        .group_by(FindingRow.pr_id)
    )
    async with db_session() as s:
        results = (await s.execute(agg_stmt)).all()

    out: dict[uuid.UUID, tuple[int, str | None]] = {}
    for pr_id, count, max_rank in results:
        severity = {3: "high", 2: "medium", 1: "low"}.get(int(max_rank or 0))
        out[pr_id] = (int(count), severity)
    return out


async def refresh_ticket_findings_summary(
    ticket_id: uuid.UUID,
    pr_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
    session,  # type: ignore[no-untyped-def]
) -> None:
    """Recompute findings rollup for *pr_id* and write it to the ticket row.

    Runs inside the caller's session; caller commits. Invoked after findings
    are posted (review end) and after an ack/push-back in reviewer/web.py.
    """
    from app.domain.tickets import update_findings_summary  # noqa: PLC0415

    rollup = await aggregate_findings_by_prs([pr_id], org_id=org_id)
    count, severity = rollup.get(pr_id, (0, None))
    await update_findings_summary(
        ticket_id,
        findings_count=count,
        max_severity=severity,
        session=session,
    )


def review_summary(aggregate: PRReviewAggregate, review: Review) -> dict[str, int]:
    """Counters for the per-review section header: N new, M re-observed, K resolved."""
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
