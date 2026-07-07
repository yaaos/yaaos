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


class RevisionContext(BaseModel, frozen=True):
    """Carries a re-entry's instruction/gap/fix text plus the stage's own
    prior artifact body."""

    source: Literal["instruction", "send_back", "fix"]
    text: str
    prior_artifact: str


class Kickoff(BaseModel, frozen=True):
    """The intake+actor+input that started a run. The ticket (with title)
    exists before the run — intake creates/targets it."""

    intake_point_id: str
    actor: Actor
    input_text: str | None
    pr_base_sha: str | None = None
    pr_head_sha: str | None = None
    notify_user_ids: tuple[UUID, ...] = ()
    # Single-use carrier for a re-entry's revision text across an async
    # provision hop: `start_rerun_from_stage` stashes it here at run
    # creation so the provision-terminal handler (`engine._start_stage_impl`)
    # can thread it onto the starting stage's FIRST dispatch, then clears it
    # (rewrites `run.kickoff`) so later stages/re-provisions never see it.
    revision: RevisionContext | None = None


class PauseResolution(BaseModel, frozen=True):
    """The `resolve_pause` request body shape."""

    action: Literal["approve", "instruct", "send_back", "kill"]
    instruction: str | None = None
    send_back_to_stage: str | None = None


# ---------------------------------------------------------------------------
# Stage-invocation context — engine-assembled input to every skill/review
# invocation. `upstream_stages` is structurally present but stays empty
# until the `context_stages`-filtered upstream-artifact offering exists; the
# shape is fixed now so `Invocation.context` never needs a breaking change.
# ---------------------------------------------------------------------------


class UpstreamStageRef(BaseModel, frozen=True):
    """One upstream stage's produced artifact, offered to a later stage's
    invocation context (filtered by `context_stages`). Not populated yet —
    always empty on `StageInvocationContext` until that wiring lands."""

    stage_name: str
    description: str
    artifact_id: UUID
    artifact_body: str


class PRContext(BaseModel, frozen=True):
    """How review skills know what to diff. Assembled from the ticket + the
    run's kickoff, not pipeline config (`engine._build_pr_context`) — `None`
    when the ticket has no PR or this run's own kickoff didn't pin a head
    SHA (e.g. a non-PR-triggered run on a PR ticket)."""

    pr_external_id: str
    head_sha: str
    base_sha: str
    prev_reviewed_head_sha: str | None = None


class PriorFindingRef(BaseModel, frozen=True):
    """One durable finding offered to a review invocation's context, by id."""

    finding_id: UUID
    severity: str
    body: str
    code_file: str | None = None
    code_line: int | None = None
    artifact_section: str | None = None


class StageInvocationContext(BaseModel, frozen=True):
    """Engine-assembled input to every skill/review invocation — rides
    `Invocation.context` (via `.model_dump(mode="json")`); the output schema
    is injected separately (`SkillReturn.model_json_schema()`), not carried
    on this type."""

    ticket_id: UUID
    stage_name: str
    branch_name: str
    input: str
    pr: PRContext | None = None
    upstream_stages: tuple[UpstreamStageRef, ...] = ()
    revision: RevisionContext | None = None
    prior_findings: tuple[PriorFindingRef, ...] = ()
    artifact_path: str


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
