"""Value objects + enums for the reviewer.

Immutable. Pure data. No I/O.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

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


# ── Reviewer-domain types (moved from core/coding_agent) ─────────────────────


class ReviewContext(BaseModel):
    """Context for a remote PR review dispatch.

    Carries the identifiers the skill needs to run `git diff base..head` in
    the clone and emit structured findings. No diff blob crosses the wire —
    the skill computes it from the clone.

    `output_schema` is an immutable snapshot of `finding_output_schema()`
    frozen at dispatch, so the skill validates against the exact shape the
    run launched with (no mid-run drift). `PostFindings` re-validates the
    returned stdout against the same contract.
    """

    org_id: uuid.UUID
    repo_external_id: str
    pr_external_id: str
    head_sha: str
    base_sha: str
    output_schema: Mapping[str, Any] = {}


class ReportedFinding(BaseModel):
    """One raw finding produced by an agent task.

    Raw strings only — no enum validation. `domain/reviewer` validates
    `severity` and `confidence` into typed values when converting to `Finding`.
    `file` and `line` are optional — general (PR-wide) findings carry no anchor.
    """

    file: str | None = None
    line: int | None = None
    category: str
    severity: str
    confidence: str
    rationale: str
    rule_violated: str
    rule_source: str
    suggested_fix: str


class _ReportedFindingDto(BaseModel):
    """The agent's per-finding output shape used for JSON schema generation.

    `severity` and `confidence` are strict enum strings — the canonical contract
    the agent must emit. `ReportedFinding` is the lenient raw-string parse twin
    used after the fact.
    """

    file: str | None = None
    line: int | None = None
    category: str
    severity: Literal["blocker", "should_fix", "nit"]
    confidence: Literal["verified", "plausible", "speculative"]
    rationale: str
    rule_violated: str
    rule_source: str
    suggested_fix: str


class FindingDraftList(BaseModel):
    """Full-review + incremental-review response: a flat list of findings.
    The agent is told to respond with `{"findings": [...]}`."""

    findings: list[_ReportedFindingDto]


def finding_output_schema() -> dict[str, Any]:
    """The canonical finding output contract as a JSON schema dict.

    Single source of truth: generated from `FindingDraftList.model_json_schema()`.
    Consumers: the skill-invocation prompt (schema appendix) and the skills
    popover endpoint. `ReportedFinding` is the lenient raw-string parse twin;
    a unit test pins its field set to this schema.
    """
    return FindingDraftList.model_json_schema()  # type: ignore[return-value]


def parse_review_output(stdout: str) -> list[ReportedFinding]:
    """Parse the agent's stream-json stdout into `ReportedFinding` objects.

    Finds the terminal `type=result` event, extracts the `result` field,
    and parses the JSON payload against `FindingDraftList`. Raises `ValueError`
    on any parse failure or structurally non-conforming output so `PostFindings`
    can gate on it.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError:
            continue

    result_event = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result_event is None:
        raise ValueError("no 'type=result' event found in stdout")
    raw_result = result_event.get("result", "")
    if not isinstance(raw_result, str):
        raise ValueError(f"result field is not a string: {type(raw_result)}")
    try:
        parsed_dict = json.loads(raw_result)
        parsed = FindingDraftList.model_validate(parsed_dict)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"agent output did not match FindingDraftList: {exc}") from exc
    return [
        ReportedFinding(
            file=d.file,
            line=d.line,
            category=d.category,
            severity=d.severity,
            confidence=d.confidence,
            rationale=d.rationale,
            rule_violated=d.rule_violated,
            rule_source=d.rule_source,
            suggested_fix=d.suggested_fix,
        )
        for d in parsed.findings
    ]
