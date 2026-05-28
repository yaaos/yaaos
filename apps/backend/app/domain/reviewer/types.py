"""Value objects + enums for the durable-findings reviewer.

Immutable. Pure data. No I/O. The aggregate (`aggregate.py`) consumes these;
the SQLAlchemy repository (`repository.py`) maps them to/from rows.

`FindingFingerprint` and `CodeAnchor` carry the cross-review identity logic;
their hash recipes live in `fingerprint.py` + `anchor.py` so this file stays
declarative.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

Severity = Literal["blocker", "major", "minor", "nit"]
AckKind = Literal["intentional", "wontfix"]
ReplyIntent = Literal[
    "acknowledgment_clear",
    "acknowledgment_unclear",
    "verify_fix",
    "question",
    "other",
]
AuthorKind = Literal["yaaos", "human"]


class FindingState(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED_CONFIRMED = "resolved_confirmed"
    RESOLVED_UNVERIFIED = "resolved_unverified"
    STALE = "stale"

    @property
    def is_terminal(self) -> bool:
        """Acknowledged + resolved + stale are terminal in this PR."""
        return self in {
            FindingState.ACKNOWLEDGED,
            FindingState.RESOLVED_CONFIRMED,
            FindingState.RESOLVED_UNVERIFIED,
            FindingState.STALE,
        }


class ReviewTrigger(StrEnum):
    """Why the review was scheduled. POC subset."""

    PR_READY = "pr_ready"
    PUSH_INCREMENTAL = "push_incremental"
    MANUAL_FULL = "manual_full"


class ReviewScopeKind(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True)
class ReviewScope:
    """`Full(base..head)` or `Incremental(prev_sha..head)`."""

    kind: ReviewScopeKind
    base_sha: str  # `base..head` start (Full) OR `prev..head` start (Incremental)
    head_sha: str

    @classmethod
    def full(cls, base_sha: str, head_sha: str) -> ReviewScope:
        return cls(kind=ReviewScopeKind.FULL, base_sha=base_sha, head_sha=head_sha)

    @classmethod
    def incremental(cls, prev_sha: str, head_sha: str) -> ReviewScope:
        return cls(kind=ReviewScopeKind.INCREMENTAL, base_sha=prev_sha, head_sha=head_sha)


@dataclass(frozen=True)
class CodeAnchor:
    """Where a finding lives. Resolves to a new line after the file changes.

    `surrounding_content_hash` covers 3 lines of context above + the anchored
    range + 3 lines below, whitespace-normalized. Used to re-find the anchor
    when line numbers drift in a later commit.

    `original_lines` snapshots the exact anchored lines at finding-creation
    time. verify_fix compares this against the current code at
    the resolved anchor to decide whether the developer's claimed fix is
    real. Empty list is allowed for rows that don't carry the field.
    """

    file_path: str
    line_start: int
    line_end: int
    surrounding_content_hash: str
    commit_sha: str
    original_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class FindingFingerprint:
    """Conceptual identity across reviews.

    Two raw findings with the same fingerprint are the same `Finding`.
    Hash recipe in `fingerprint.py`.
    """

    file_path: str
    rule_id: str
    anchor_content_hash: str  # sha256 of the anchored line content
    body_gist_hash: str  # sha256 of normalized `(rule_id, title)`

    @property
    def hash(self) -> str:
        """Single string used as the DB fingerprint_hash and dedup key."""
        return f"{self.file_path}|{self.rule_id}|{self.anchor_content_hash}|{self.body_gist_hash}"


@dataclass
class Finding:
    """Aggregate-managed finding entity."""

    id: uuid.UUID
    pr_id: uuid.UUID
    org_id: uuid.UUID
    fingerprint: FindingFingerprint
    rule_id: str
    title: str
    body: str
    rationale: str
    concrete_failure_scenario: str
    confidence: int  # 0-100; max(stored, new) across re-observations
    severity: Severity  # sticky; never escalates
    state: FindingState
    current_anchor: CodeAnchor
    source_agent: str
    first_seen_review_id: uuid.UUID
    last_observed_review_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


@dataclass
class FindingObservation:
    """Append-only sighting row."""

    id: uuid.UUID
    finding_id: uuid.UUID
    review_id: uuid.UUID
    anchor: CodeAnchor
    raw_body: str
    created_at: datetime


@dataclass
class Review:
    """One review run on a PR."""

    id: uuid.UUID
    pr_id: uuid.UUID
    org_id: uuid.UUID
    sequence_number: int  # 1, 2, 3, ... per PR
    trigger_reason: ReviewTrigger
    scope: ReviewScope
    commit_sha_at_start: str
    status: str  # queued | running | done | failed | superseded
    superseded_by_review_id: uuid.UUID | None
    pending_replay: bool
    created_at: datetime


@dataclass
class AcknowledgmentDecision:
    """Persistent dev intent to skip a finding (intentional | wontfix)."""

    id: uuid.UUID
    finding_id: uuid.UUID
    kind: AckKind
    rationale: str
    made_by_external_id: str
    made_by_message_id: uuid.UUID
    created_at: datetime


@dataclass
class CommentMessage:
    """One yaaos/human message in a thread."""

    id: uuid.UUID
    thread_id: uuid.UUID
    author_kind: AuthorKind
    author_external_id: str
    external_comment_id: str
    in_reply_to_external_id: str | None
    body: str
    classified_intent: ReplyIntent | None
    created_at: datetime


@dataclass
class CommentThread:
    """1:1 with `Finding`. Carries the GitHub-side thread id when known."""

    id: uuid.UUID
    finding_id: uuid.UUID
    external_thread_id: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[CommentMessage] = field(default_factory=list)
