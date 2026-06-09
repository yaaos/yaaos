"""Value objects + enums for the reviewer.

Immutable. Pure data. No I/O.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Severity = Literal["blocker", "should_fix", "nit"]
Confidence = Literal["verified", "plausible", "speculative"]


class ReviewTrigger:
    PR_READY = "pr_ready"
    PUSH_INCREMENTAL = "push_incremental"
    MANUAL_FULL = "manual_full"


class ReviewScopeKind:
    FULL = "full"
    INCREMENTAL = "incremental"


@dataclass(frozen=True)
class ReviewScope:
    """`Full(base..head)` or `Incremental(prev_sha..head)`."""

    kind: str
    base_sha: str
    head_sha: str

    @classmethod
    def full(cls, base_sha: str, head_sha: str) -> ReviewScope:
        return cls(kind=ReviewScopeKind.FULL, base_sha=base_sha, head_sha=head_sha)

    @classmethod
    def incremental(cls, prev_sha: str, head_sha: str) -> ReviewScope:
        return cls(kind=ReviewScopeKind.INCREMENTAL, base_sha=prev_sha, head_sha=head_sha)


@dataclass
class Finding:
    """One persisted finding produced by a review run.

    `finding_display_id` is a per-PR monotonic integer assigned at creation;
    the user-visible handle is `<category-prefix>-<finding_display_id>`.
    `file` and `line` are optional — general (PR-wide) findings carry no anchor.
    """

    id: uuid.UUID
    pr_id: uuid.UUID
    org_id: uuid.UUID
    review_id: uuid.UUID
    finding_display_id: int
    category: str
    severity: Severity
    confidence: Confidence
    rationale: str
    rule_violated: str
    rule_source: str
    suggested_fix: str
    file: str | None
    line: int | None
    created_at: datetime
    updated_at: datetime


@dataclass
class Review:
    """One review run on a PR."""

    id: uuid.UUID
    pr_id: uuid.UUID
    org_id: uuid.UUID
    sequence_number: int  # 1, 2, 3, ... per PR
    trigger_reason: str
    scope: ReviewScope
    commit_sha_at_start: str
    status: str  # queued | running | done | failed
    created_at: datetime
