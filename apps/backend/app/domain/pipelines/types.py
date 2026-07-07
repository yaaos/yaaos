"""Value objects owned by `domain/pipelines`.

The definition model (`PipelineDefinition` + `Stage` union) plus the
flatten/validate logic over it live in `definition.py`. This module carries
the stored-entity wrappers (`Pipeline`, `PipelineSummary`) and the
run/stage read-model VOs the Runs tab consumes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.core.audit_log import Actor
from app.domain.findings import Finding
from app.domain.pipelines.definition import PipelineDefinition

RunState = Literal["queued", "running", "paused", "completed", "failed", "killed", "cancelled"]
RunPhase = Literal["provision", "stages", "cleanup"]
StageKind = Literal["skill", "review", "action", "system"]
StageStatus = Literal["running", "completed", "failed"]
StagePhase = Literal["main", "review", "fix"]
Confidence = Literal["low", "medium", "high"]
BoundaryOutcome = Literal["proceeded", "paused", "sent_back"]


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
