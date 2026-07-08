"""domain/pipelines — the run engine: data-defined pipelines, run + stage
lifecycle, HITL pauses.

One `PipelineDefinition` (discriminated `skill | review | action | call`
stages) per org pipeline, executed by a generic run/stage dispatcher.
See `apps/backend/docs/domain_pipelines.md`.
"""

# Side-effect imports: registers /api/pipelines/* routes, and registers the
# pipeline_schedule_tick + resume_stalled_runs `@scheduled` jobs with core/tasks.
import app.domain.pipelines.scheduler_jobs
import app.domain.pipelines.web  # noqa: F401
from app.core.agent_gateway import register_agent_event_consumer as _register_agent_event_consumer
from app.core.intake import IntakePoint, register_intake_point
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
from app.domain.pipelines.engine import HANDLE_AGENT_EVENT as _HANDLE_AGENT_EVENT
from app.domain.pipelines.engine import register_comment_findings_provider, register_run_terminal_hook
from app.domain.pipelines.service import (
    InvalidPauseResolutionError,
    MissingInheritedArtifactError,
    NotEscalationTargetError,
    PauseAlreadyResolvedError,
    PauseNotFoundError,
    PipelineNameTakenError,
    PipelineNotFoundError,
    PipelineReferencedError,
    RunAlreadyTerminalError,
    RunNotFoundError,
    StageNotInDefinitionError,
    TemplateNotFoundError,
    create_pipeline,
    delete_pipeline,
    get_pipeline,
    has_run_in_flight,
    instantiate_template,
    list_pipelines,
    list_templates,
    pipeline_referenced_by_call,
    request_cancel,
    resolve_pause,
    start_rerun_from_stage,
    start_run,
    update_pipeline,
)
from app.domain.pipelines.service import _lookup_pipeline_ref as _lookup_pipeline_ref
from app.domain.pipelines.types import (
    Kickoff,
    PauseResolution,
    Pipeline,
    PipelineRun,
    PipelineSummary,
    RunOverview,
    StageExecution,
)
from app.domain.pipelines.views import get_run_overview, list_runs_for_ticket
from app.domain.repos import register_pipeline_lookup as _register_pipeline_lookup

__all__ = [
    "ActionStage",
    "BoundaryControl",
    "InvalidPauseResolutionError",
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
    "RunAlreadyTerminalError",
    "RunNotFoundError",
    "RunOverview",
    "SkillStage",
    "Stage",
    "StageExecution",
    "StageNotInDefinitionError",
    "TemplateNotFoundError",
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
    "register_comment_findings_provider",
    "register_run_terminal_hook",
    "request_cancel",
    "resolve_pause",
    "start_rerun_from_stage",
    "start_run",
    "update_pipeline",
]

# Register into the shared agent-event consumer registry (see
# `core/agent_gateway.register_agent_event_consumer`) — keeps `core/agent_gateway`
# free of any import on `domain/pipelines`.
_register_agent_event_consumer(_HANDLE_AGENT_EVENT)

# `domain/repos` can't import this module directly (it already depends on
# `domain/repos` for `pipeline_referenced_by_binding`; the reverse edge would
# cycle) — hand it a lookup callable instead. See
# `domain/repos/service.py`'s module docstring.
_register_pipeline_lookup(_lookup_pipeline_ref)

# First-party schedule intake point — `domain/repos` trigger bindings target
# this id for cron-fired pipeline runs. No firing tick consumes it yet; the
# point is registered so it's selectable in the Repos-page trigger picker and
# passes `add_binding`'s intake_point_id check.
register_intake_point(IntakePoint(id="schedule", plugin_id=None, label="Schedule", kind="schedule"))
