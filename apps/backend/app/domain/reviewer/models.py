"""SQLAlchemy models for the reviewer's persistent state.

Two generations coexist here while plan/notes/full-pr-flow.md §13 is in flight:

- Generation 1 (today): `ReviewJobRow` + `PostedCommentRow` — one review-job row
  per (PR x run), JSONB findings list, separate posted_comments table.
- Generation 2 (in progress): `FindingRow` + `FindingObservationRow` +
  `AcknowledgmentDecisionRow` + `CommentThreadRow` + `CommentMessageRow` —
  findings as first-class entities with a state machine, append-only
  observation history, persistent acknowledgments, and 1:1 comment threads.

Generation 1 keeps working until the §13 step 7 cut-over wires the aggregate
through `schedule_review`. At that point `review_jobs` is renamed `reviews`,
the JSONB findings column drops, and `posted_comments` is dropped (subsumed
by `comment_messages`). For POC, generation-2 tables carry `review_id` as an
unconstrained UUID column to keep the rename cheap.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    REAL,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReviewJobRow(Base):
    __tablename__ = "review_jobs"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    # Why this review was scheduled. Values: `pr_ready`, `pr_synchronized`,
    # `rereview_command`, `ui_rereview`. Future: `implementer_loop` once an
    # implementer module exists to call `run_review`.
    triggered_by: Mapped[str] = mapped_column(String, nullable=False, server_default="pr_ready")
    # Where the review result went. `vcs` (today: posted via the VCS plugin).
    # Future: `caller` when `run_review` returns findings without posting.
    destination: Mapped[str] = mapped_column(String, nullable=False, server_default="vcs")
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
    # Each finding object carries a `source_agent` field naming which subagent
    # surfaced it (e.g. "yaaos-architecture").
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
        Index("ix_review_jobs_pr_status_created", "pr_id", "status", "created_at"),
        Index("ix_review_jobs_status_heartbeat", "status", "last_heartbeat_at"),
    )


class PostedCommentRow(Base):
    __tablename__ = "posted_comments"

    external_comment_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    review_job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("review_jobs.id"), nullable=False
    )
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ── Generation 2 — durable findings + state machine ─────────────────────────
# Plan/notes/full-pr-flow.md §4.1. These tables are populated by the aggregate
# (§13 steps 5-7); generation-1 ReviewJobRow continues to record the run-level
# state (status, heartbeat, model/effort) and will be renamed `reviews` when
# the aggregate cut-over lands.


class FindingRow(Base):
    """First-class finding. Durable per PR; survives re-reviews via fingerprint match.

    `state` follows the FindingState machine (open → acknowledged | resolved_* | stale).
    `current_anchor` is the latest CodeAnchor (file, line range, surrounding hash,
    commit_sha); see plan §2.3. `severity` is sticky across re-observations and
    never escalates; `confidence` is `max(stored, new)` (plan §10.10).
    """

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    pr_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pull_requests.id"), nullable=False
    )
    fingerprint_hash: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(String, nullable=False)
    # Required per plan §10.1; finding is dropped before insert when missing.
    concrete_failure_scenario: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    current_anchor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_agent: Mapped[str] = mapped_column(String, nullable=False)
    # Unconstrained UUID by design — generation-1 review_jobs is renamed to
    # `reviews` in §13 step 7. Adding an FK now would just need migrating then.
    first_seen_review_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    last_observed_review_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
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

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("findings.id"), nullable=False
    )
    review_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    anchor: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_finding_observations_finding_review", "finding_id", "review_id"),)


class CommentThreadRow(Base):
    """1:1 with FindingRow. Created on first posted comment for the finding."""

    __tablename__ = "comment_threads"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    `classified_intent` + `classification_confidence` populated only for human
    messages, by the §6.4 reply-classifier path.
    """

    __tablename__ = "comment_messages"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("comment_threads.id"), nullable=False
    )
    author_kind: Mapped[str] = mapped_column(String, nullable=False)  # yaaos | human
    author_external_id: Mapped[str] = mapped_column(String, nullable=False)
    external_comment_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    in_reply_to_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(String, nullable=False)
    classified_intent: Mapped[str | None] = mapped_column(String, nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(REAL, nullable=True)
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

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
