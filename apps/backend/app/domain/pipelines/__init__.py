"""domain/pipelines — the run engine: data-defined pipelines, run + stage
lifecycle, HITL pauses.

Replaces `core/workflow` + `domain/reviewer`'s workflow-engine role: one
`PipelineDefinition` (discriminated `skill | review | action | call` stages)
per org pipeline, executed by a generic run/stage dispatcher instead of
per-step command classes. See `apps/backend/docs/domain_pipelines.md`.
"""

# Side-effect import: registers /api/pipelines/* routes.
import app.domain.pipelines.web  # noqa: F401
from app.domain.pipelines.definition import (
    ActionStage,
    BoundaryControl,
    PipelineCallStage,
    PipelineDefinition,
    PipelineValidationError,
    ReviewConfig,
    ReviewSkillStage,
    SkillStage,
    Stage,
)
from app.domain.pipelines.service import (
    MissingInheritedArtifactError,
    NotEscalationTargetError,
    PauseAlreadyResolvedError,
    PauseNotFoundError,
    PipelineNameTakenError,
    PipelineNotFoundError,
    PipelineReferencedError,
    RunNotFoundError,
    StageNotInDefinitionError,
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    get_run_overview,
    has_run_in_flight,
    instantiate_template,
    list_pipelines,
    list_runs_for_ticket,
    list_templates,
    pipeline_referenced_by_call,
    request_cancel,
    resolve_pause,
    start_rerun_from_stage,
    start_run,
    update_pipeline,
)
from app.domain.pipelines.types import (
    Kickoff,
    PauseResolution,
    Pipeline,
    PipelineRun,
    PipelineSummary,
    RunOverview,
    StageExecution,
)

__all__ = [
    "ActionStage",
    "BoundaryControl",
    "Kickoff",
    "MissingInheritedArtifactError",
    "NotEscalationTargetError",
    "PauseAlreadyResolvedError",
    "PauseNotFoundError",
    "PauseResolution",
    "Pipeline",
    "PipelineCallStage",
    "PipelineDefinition",
    "PipelineNameTakenError",
    "PipelineNotFoundError",
    "PipelineReferencedError",
    "PipelineRun",
    "PipelineSummary",
    "PipelineValidationError",
    "ReviewConfig",
    "ReviewSkillStage",
    "RunNotFoundError",
    "RunOverview",
    "SkillStage",
    "Stage",
    "StageExecution",
    "StageNotInDefinitionError",
    "create_pipeline",
    "delete_pipeline",
    "get_pipeline",
    "get_run_overview",
    "has_run_in_flight",
    "instantiate_template",
    "list_pipelines",
    "list_runs_for_ticket",
    "list_templates",
    "pipeline_referenced_by_call",
    "request_cancel",
    "resolve_pause",
    "start_rerun_from_stage",
    "start_run",
    "update_pipeline",
]
