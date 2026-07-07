"""Skill contracts + confidence-bucket policy for `domain/pipelines`.

`SkillReturn` is the main-skill `ExpectedResponse` — the schema injected into
every skill-stage `Invocation.context["output_schema"]` and validated against
the terminal event's parsed JSON output (`outputs["output"]`). `SkillReviewReturn`
is the review-skill `ExpectedResponse` — dispatched over a produced artifact
(the review loop attached to a `SkillStage`) or standalone (`ReviewSkillStage`,
`kind='review'`). Facts only: the skill never labels a finding
fixed/residual itself — `prior_finding_verdicts` carries the skill's
per-finding assertion, and the engine applies the mechanical verdict matrix
(`domain/findings.resolve`/`reflag`/`reopen`/`dismiss`).

Confidence is a rubric-anchored integer (0-100) the skill reports; the engine
buckets it into `low | medium | high` for everything user-facing while the
raw int is retained on `stage_executions.loop_state` for insight mining.
Cutoffs are chosen knowing verbalized confidence scores cluster 80-100.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# < LOW_CONFIDENCE_MAX -> low; < HIGH_CONFIDENCE_MIN -> medium; else high.
LOW_CONFIDENCE_MAX = 50
HIGH_CONFIDENCE_MIN = 90

Confidence = Literal["low", "medium", "high"]


def bucket_confidence(value: int) -> Confidence:
    """Bucket a raw 0-100 confidence score. Policy lives here, not in the
    skill prompt — cutoffs can move without touching any skill contract."""
    if value < LOW_CONFIDENCE_MAX:
        return "low"
    if value < HIGH_CONFIDENCE_MIN:
        return "medium"
    return "high"


class SkillReturn(BaseModel, frozen=True, extra="forbid"):
    """Main-skill structured output — the only stage `ExpectedResponse`.

    `send_back`/`cannot_complete` are handled as run-failure placeholders
    until the boundary/send-back machinery exists — a stage returning either
    outcome fails the run with `outcome_reason` as the failure reason rather
    than being silently accepted.
    """

    outcome: Literal["completed", "cannot_complete", "send_back"]
    outcome_reason: str | None = None
    send_back_to_stage: str | None = None
    confidence: int = Field(ge=0, le=100)
    paths_affected: list[str] = Field(default_factory=list)
    summary: str


class SkillReviewFinding(BaseModel, frozen=True, extra="forbid"):
    """Facts only — the skill never labels fixed/residual. `defect_in_artifact`
    is exceptional: the stage-name key of the INPUT artifact the skill was
    shown that contains the root cause. An unknown name (not one of the
    stage's `upstream_stages`) degrades to a plain residual — logged, not a
    contract violation."""

    severity: Literal["blocker", "should_fix", "nit"]
    body: str
    code_file: str | None = None
    code_line: int | None = None
    artifact_section: str | None = None
    defect_in_artifact: str | None = None


class PriorFindingVerdict(BaseModel, frozen=True, extra="forbid"):
    """One assertion about a finding the skill was shown as `prior_findings`.
    `status=None` means no status assertion (reply-only, e.g. answering a
    question) — the engine applies no transition for it."""

    finding_id: UUID
    status: Literal["fixed", "still_present", "user_overrode"] | None = None
    reply: str | None = None


class SkillReviewReturn(BaseModel, frozen=True, extra="forbid"):
    """Review-skill structured output — the `ExpectedResponse` for both a
    `SkillStage`'s attached review loop and a standalone `ReviewSkillStage`."""

    new_findings: list[SkillReviewFinding]
    prior_finding_verdicts: list[PriorFindingVerdict] = Field(default_factory=list)
    confidence: int = Field(ge=0, le=100)
    summary: str
