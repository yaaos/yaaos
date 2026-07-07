"""Value objects owned by `domain/pipelines`.

The definition model (`PipelineDefinition` + `Stage` union) is the wire +
storage shape edited by the Pipelines page; `PipelineRun` / `StageExecution`
are the read-model VOs the Runs tab consumes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.audit_log import Actor
from app.domain.findings import Finding

RunState = Literal["queued", "running", "paused", "completed", "failed", "killed", "cancelled"]
RunPhase = Literal["provision", "stages", "cleanup"]
StageKind = Literal["skill", "review", "action", "system"]
StageStatus = Literal["running", "completed", "failed"]
StagePhase = Literal["main", "review", "fix"]
Confidence = Literal["low", "medium", "high"]
BoundaryOutcome = Literal["proceeded", "paused", "sent_back"]


# ---------------------------------------------------------------------------
# Definition model (wire + storage shape)
# ---------------------------------------------------------------------------


class BoundaryControl(BaseModel, frozen=True):
    """Flat per-stage "what to do next" setting; `on_*` evaluated only when
    `mode == "conditional"`."""

    mode: Literal["always_hitl", "always_proceed", "conditional"] = "always_hitl"
    on_blocker_residuals: bool = False
    on_should_fix_residuals: bool = False
    on_protected_code: bool = False
    on_confidence_below: Literal["medium", "high"] | None = None


class ReviewConfig(BaseModel, frozen=True):
    """Review skill name + max iterations; `None` on the owning stage means
    review is off."""

    skill_name: str
    max_iterations: int = Field(ge=1, le=3)
    finding_prefix: str | None = None


class SkillStage(BaseModel, frozen=True):
    """A main-skill invocation stage; optionally carries a review loop."""

    kind: Literal["skill"] = "skill"
    id: UUID
    name: str
    description: str = ""
    skill_name: str
    coding_agent_plugin_id: str
    model: str
    effort: str
    review: ReviewConfig | None = None
    context_stages: tuple[str, ...] | None = None
    wallclock_seconds: int = 3600
    boundary: BoundaryControl


class ReviewSkillStage(BaseModel, frozen=True):
    """A stage whose main invocation speaks the review contract directly —
    produces findings, no artifact, structurally cannot carry a review loop."""

    kind: Literal["review"] = "review"
    id: UUID
    name: str
    description: str = ""
    skill_name: str
    coding_agent_plugin_id: str
    model: str
    effort: str
    finding_prefix: str | None = None
    context_stages: tuple[str, ...] | None = None
    wallclock_seconds: int = 3600
    boundary: BoundaryControl


class ActionStage(BaseModel, frozen=True):
    """A synchronous control-plane action stage."""

    kind: Literal["action"] = "action"
    id: UUID
    description: str = ""
    action_id: str


class PipelineCallStage(BaseModel, frozen=True):
    """Calls another org pipeline; expands recursively at flatten time."""

    kind: Literal["call"] = "call"
    id: UUID
    description: str = ""
    pipeline_id: UUID


Stage = Annotated[
    SkillStage | ReviewSkillStage | ActionStage | PipelineCallStage, Field(discriminator="kind")
]


class PipelineDefinition(BaseModel, frozen=True):
    """The authored content — what POST/PUT accept. Shipped defaults are code
    instances with pinned uuid7 ids."""

    id: UUID
    name: str
    description: str = ""
    stages: tuple[Stage, ...]


class Pipeline(BaseModel, frozen=True):
    """The stored org entity — what `get_pipeline` returns."""

    definition: PipelineDefinition
    updated_at: datetime
    updated_by_login: str | None
    referenced: bool


class PipelineSummary(BaseModel, frozen=True):
    """One `list_pipelines` element — definition rows, unflattened."""

    id: UUID
    name: str
    stage_count: int
    updated_at: datetime
    updated_by_login: str | None
    referenced: bool


class Kickoff(BaseModel, frozen=True):
    """The intake+actor+input that started a run. The ticket (with title)
    exists before the run — intake creates/targets it."""

    intake_point_id: str
    actor: Actor
    input_text: str | None
    pr_base_sha: str | None = None
    pr_head_sha: str | None = None
    notify_user_ids: tuple[UUID, ...] = ()


class PauseResolution(BaseModel, frozen=True):
    """The `resolve_pause` request body shape."""

    action: Literal["approve", "instruct", "send_back", "kill"]
    instruction: str | None = None
    send_back_to_stage: str | None = None


# ---------------------------------------------------------------------------
# Run + stage read models
# ---------------------------------------------------------------------------


class RunKickoffView(BaseModel, frozen=True):
    """Read-model projection of a run's `Kickoff` for the Runs tab."""

    intake_point_id: str
    actor_kind: str
    actor_login: str | None
    input_text: str | None


class Decision(BaseModel, frozen=True):
    """One pause resolution recorded against a stage execution."""

    action: str
    actor_login: str | None
    instruction: str | None
    resolved_at: datetime


class StageExecution(BaseModel, frozen=True):
    """One stage-execution attempt, read-model shape for the Runs tab."""

    stage_index: int | None
    kind: StageKind
    stage_name: str
    status: str
    confidence: Confidence | None
    review_iterations: int
    boundary_outcome: BoundaryOutcome | None
    artifact_ids: tuple[UUID, ...]
    action_result: dict[str, Any] | None
    decisions: tuple[Decision, ...]
    failure_reason: str | None
    started_at: datetime
    completed_at: datetime | None


class PipelineRun(BaseModel, frozen=True):
    """Replaces WorkflowExecution — the Runs-tab timeline entry."""

    id: UUID
    pipeline_name: str
    state: RunState
    kickoff: RunKickoffView
    created_at: datetime
    completed_at: datetime | None
    failure_reason: str | None
    stages: tuple[StageExecution, ...]


class PauseDetail(BaseModel, frozen=True):
    """Overview-tab payload for a `paused` run."""

    pause_id: UUID
    stage_name: str
    tripped: dict[str, Any]
    artifact_id: UUID | None
    residuals: tuple[Finding, ...]
    escalation_logins: tuple[str, ...]
    can_respond: bool


class RunOutcome(BaseModel, frozen=True):
    """Overview-tab payload for a `terminal` run."""

    state: RunState
    pr_url: str | None
    failure_reason: str | None


class RunOverview(BaseModel, frozen=True):
    """Server-computed Overview-tab payload, tagged on `status`."""

    status: Literal["paused", "in_flight", "terminal"]
    pause: PauseDetail | None = None
    run: PipelineRun | None = None
    outcome: RunOutcome | None = None
