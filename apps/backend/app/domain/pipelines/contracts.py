"""Skill contracts + confidence-bucket policy for `domain/pipelines`.

`SkillReturn` is the main-skill `ExpectedResponse` — the schema injected into
every skill-stage `Invocation.context["output_schema"]` and validated against
the terminal event's parsed JSON output (`outputs["output"]`). The review
contract (`SkillReviewReturn` and friends) lands with the review loop.

Confidence is a rubric-anchored integer (0-100) the skill reports; the engine
buckets it into `low | medium | high` for everything user-facing while the
raw int is retained on `stage_executions.loop_state` for insight mining.
Cutoffs are chosen knowing verbalized confidence scores cluster 80-100.
"""

from __future__ import annotations

from typing import Literal

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
