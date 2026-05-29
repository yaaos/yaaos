"""SQLAlchemy models for the reviewer's persistent state.

- `ReviewRow` (`reviews`) — one row per PR run. Carries run-level state
  (status, heartbeat, model/effort, activity_log) PLUS the durable-findings
  view fields (`sequence_number`, `trigger_reason`, `scope_kind`, `scope_prev_sha`,
  `commit_sha_at_start`, `superseded_by_review_id`, `pending_replay`).
- `FindingRow`, `FindingObservationRow`, `CommentThreadRow`, `CommentMessageRow`,
  `AcknowledgmentDecisionRow` — durable findings + acks + threads + messages
  as first-class entities with a state machine.

Posted yaaos comments live in `comment_messages`. Findings are split
across `findings` (first-class rows) + `finding_observations` (per-run
sightings).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReviewRow(Base):
    """One review run per PR.

    Identity = `id` UUID. `sequence_number` is a per-PR ordinal (1, 2, 3, …)
    assigned at insert time inside the PG advisory lock so concurrent inserts
    serialize cleanly; the UI uses it for "Review 1 / Review 2 / …" labels.
    """

    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )

    # Per-PR ordinal, 1..N. Assigned at insert time under the advisory lock.
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")

    # Why this review was scheduled. Values:
    # `pr_ready` | `push_incremental` | `manual_full` | `pr_synchronized` |
    # `rereview_command` | `ui_rereview`. The first three are the durable-
    # findings vocabulary; the last three are accepted aliases the intake
    # paths emit.
    trigger_reason: Mapped[str] = mapped_column(String, nullable=False, server_default="pr_ready")

    # Where the review result goes. `vcs` posts findings to the PR.
    destination: Mapped[str] = mapped_column(String, nullable=False, server_default="vcs")

    # `full` (base..head) or `incremental` (prev_sha..head). `scope_prev_sha`
    # is non-null only when scope_kind = 'incremental'.
    scope_kind: Mapped[str] = mapped_column(String, nullable=False, server_default="full")
    scope_prev_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    commit_sha_at_start: Mapped[str | None] = mapped_column(String, nullable=True)

    # Used when a manual full review cancels an in-flight incremental.
    superseded_by_review_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    # Set when a push arrives during an in-flight review; the trigger policy
    # re-evaluates on completion and may schedule another incremental.
    pending_replay: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    skip_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_step: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    lessons_applied: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)), nullable=True
    )
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    review_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Denormalized cache of the vcs.Finding payloads posted for this review.
    # Durable per-finding state lives in `findings` (FindingRow); this column
    # exists so the AgentCard view (snippet + applied_lesson_ids) doesn't
    # have to reconstruct findings from anchors at read time. Written on
    # review completion; consumed by the augment-mode UI.
    findings: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # Chronological array of pre-rendered activity events captured from the
    # CLI's stream-json output. Cap 5 MB per row (enforced in app code).
    activity_log: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    # What the CLI was asked to use. `model` may be updated on completion to
    # the resolved name reported in the `result` stream event (e.g. `opus`
    # alias → `claude-opus-4-7-<date>`).
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    effort: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("pr_id", "sequence_number", name="uq_reviews_pr_sequence"),
        Index("ix_reviews_pr_status_created", "pr_id", "status", "created_at"),
        Index("ix_reviews_status_heartbeat", "status", "last_heartbeat_at"),
        Index("ix_reviews_pr_sequence", "pr_id", "sequence_number"),
    )


# ── Durable findings + state machine ─────────────────────────────────────────


class FindingRow(Base):
    """First-class finding. Durable per PR; survives re-reviews via fingerprint match.

    `state` follows the FindingState machine (open → acknowledged | resolved_* | stale).
    `current_anchor` is the latest CodeAnchor (file, line range, surrounding hash,
    commit_sha). `severity` is sticky across re-observations and
    never escalates; `confidence` is `max(stored, new)`.
    """

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    fingerprint_hash: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    # Required; finding is dropped before insert when missing.
    concrete_failure_scenario: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    current_anchor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_agent: Mapped[str] = mapped_column(String, nullable=False)
    first_seen_review_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reviews.id"), nullable=False
    )
    last_observed_review_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reviews.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("pr_id", "fingerprint_hash", name="uq_findings_pr_fingerprint"),
        Index("ix_findings_pr_state", "pr_id", "state"),
        Index("ix_findings_fingerprint", "fingerprint_hash"),
    )


class FindingObservationRow(Base):
    """Append-only row recording one (finding x review) sighting."""

    __tablename__ = "finding_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("findings.id"), nullable=False
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("reviews.id"), nullable=False
    )
    anchor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_finding_observations_finding_review", "finding_id", "review_id"),)


class CommentThreadRow(Base):
    """1:1 with FindingRow. Created on first posted comment for the finding."""

    __tablename__ = "comment_threads"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("findings.id"), nullable=False, unique=True
    )
    # GitHub review thread id when available; webhook lookup key for developer
    # replies. Indexed because intake resolves external→internal here.
    external_thread_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CommentMessageRow(Base):
    """Every message in every thread — yaaos and human alike. Append-only.

    `classified_intent` populated only for human messages, by the
    reply-classifier path. The intent label itself encodes the routing
    decision (see `domain/reviewer/llm/classifier.py`) — no separate
    confidence column.
    """

    __tablename__ = "comment_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("comment_threads.id"), nullable=False
    )
    author_kind: Mapped[str] = mapped_column(String, nullable=False)  # yaaos | human
    author_external_id: Mapped[str] = mapped_column(String, nullable=False)
    external_comment_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    in_reply_to_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(String, nullable=False)
    classified_intent: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_comment_messages_thread_created", "thread_id", "created_at"),)


class AcknowledgmentDecisionRow(Base):
    """Persistent developer decision to skip a finding (intentional | wontfix).

    Survives future reviews — when the same fingerprint reappears, the
    aggregate sees the ack and drops the new observation silently.
    """

    __tablename__ = "acknowledgment_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("findings.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)  # intentional | wontfix
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    made_by_external_id: Mapped[str] = mapped_column(String, nullable=False)
    made_by_message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("comment_messages.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
