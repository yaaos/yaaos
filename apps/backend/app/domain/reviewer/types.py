"""Value objects + enums for the reviewer.

Immutable. Pure data. No I/O.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

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


# ── Reviewer-domain types ───────────────────────────────────────────────────


class ReviewContext(BaseModel):
    """Context for a remote PR review dispatch.

    Carries the identifiers the skill needs to run `git diff base..head` in
    the clone and emit structured findings. No diff blob crosses the wire —
    the skill computes it from the clone.
    """

    org_id: uuid.UUID
    repo_external_id: str
    pr_external_id: str
    head_sha: str
    base_sha: str


class ReportedFindingShape(BaseModel):
    """One finding produced by the agent.

    Strict enum strings for `severity` and `confidence` — the canonical
    per-finding shape that `CodeReviewResponse.findings` carries. Validated
    at parse time by `CodingAgentCommand.handle_response` via
    `CodeReview.ExpectedResponse.model_validate_json`. Also the type
    `PostFindings` receives from the workflow dataflow and passes directly
    to `publish_findings`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str | None = None
    line: int | None = None
    category: str
    severity: Literal["blocker", "should_fix", "nit"]
    confidence: Literal["verified", "plausible", "speculative"]
    rationale: str
    rule_violated: str
    rule_source: str
    suggested_fix: str


class CodeReviewResponse(BaseModel):
    """Expected JSON response shape from the `pr_review` coding-agent skill.

    Declared as `CodeReview.ExpectedResponse`. The `@final dispatch` on
    `CodingAgentCommand` auto-injects `model_json_schema()` into the
    `Invocation.context["output_schema"]` slot so the skill prompt carries
    the exact contract. The engine calls `handle_response` on
    `completed_success` events and validates the agent's output against
    this model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[ReportedFindingShape]


# ── Workflow input snapshot ─────────────────────────────────────────────


class TicketSnapshot(BaseModel):
    """Immutable snapshot of a ticket + PR at workflow-start time.

    Passed as `workflow_input` to `engine.start` for the `pr_review_v1`
    workflow. The engine stores it (via `model_dump(mode='json')`) on the
    `workflow_executions.workflow_input` column and makes it accessible
    inside step `inputs_factory` lambdas via `WorkflowInputRef.outputs`.

    All 12 fields are read once at `start_pr_review` time from the ticket
    and its associated PR row — commands never query the DB for this data.
    """

    model_config = ConfigDict(frozen=True)

    ticket_id: UUID
    org_id: UUID
    plugin_id: str
    repo_external_id: str
    pr_id: UUID | None = None
    pr_external_id: str | None = None
    head_sha: str
    base_sha: str | None = None
    is_draft: bool = False
    is_fork: bool = False
    labels: tuple[str, ...] = ()
    author_login: str | None = None
