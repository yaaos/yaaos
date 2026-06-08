"""domain/coding_agent — Protocol + registry for coding-agent CLI plugins.

The Protocol exposes five task modes — `review` (full-review),
`incremental_review` (prev_sha..head only), `verify_fix` (is the finding still
present at HEAD?), `stale_check` (does the finding still apply after the code
changed?), and `answer_question` (developer asked a question on a finding;
answer it from the workspace). Plugins own prompt assembly + parsing for each
mode; consumers (today: `domain/reviewer`) hand over domain context and read
domain results.
"""

from app.core.agent_gateway import register_run_sink as _register_run_sink

# Import the partition-maintenance module for its `@scheduled` side effect —
# registers the daily `coding_agent_activity_partition_maintenance` task with
# the broker + scheduler registry at import time.
from app.domain.coding_agent import partition_maintenance as _partition_maintenance  # noqa: F401
from app.domain.coding_agent.invocation import InvocationMode, build_invocation
from app.domain.coding_agent.prompts import (
    AnswerQuestionDto,
    FindingDraftList,
    StaleCheckDto,
    VerifyFixDto,
    assemble_answer_question_prompt,
    assemble_incremental_review_prompt,
    assemble_stale_check_prompt,
    assemble_verify_fix_prompt,
    finding_output_schema,
    schema_appendix,
)
from app.domain.coding_agent.run_service import (
    create_run,
    finalize_run,
    get_run_id_for_command,
    get_run_id_for_workflow_step,
    get_step_activity,
)
from app.domain.coding_agent.run_sink_impl import CodingAgentRunSinkImpl
from app.domain.coding_agent.service import (
    CodingAgentRegistry,
    answer_question,
    bind_coding_agent_registry,
    current_coding_agent_registry,
    get_plugin,
    health_check_all,
    incremental_review,
    list_registered_plugins,
    register_coding_agent_plugin,
    register_plugin,
    registered_plugin_ids,
    review,
    stale_check,
    validate_config,
    verify_fix,
)
from app.domain.coding_agent.types import (
    ActivityEvent,
    ActivityLog,
    AnswerQuestionContext,
    AnswerQuestionResult,
    CodingAgentCacheMiss,
    CodingAgentError,
    CodingAgentPlugin,
    ExecSpec,
    FindingAnchor,
    HealthStatus,
    IncrementalReviewContext,
    IncrementalReviewResult,
    Invocation,
    InvocationStatus,
    InvocationTelemetry,
    OnActivity,
    PluginNotFoundError,
    PriorThreadMessage,
    ReportedFinding,
    ReviewContext,
    ReviewResult,
    Severity,
    StaleCheckContext,
    StaleCheckResult,
    Usage,
    ValidationResult,
    VerifyFixContext,
    VerifyFixResult,
)

_register_run_sink(CodingAgentRunSinkImpl())

__all__ = [
    "ActivityEvent",
    "ActivityLog",
    "AnswerQuestionContext",
    "AnswerQuestionDto",
    "AnswerQuestionResult",
    "CodingAgentCacheMiss",
    "CodingAgentError",
    "CodingAgentPlugin",
    "CodingAgentRegistry",
    "CodingAgentRunSinkImpl",
    "ExecSpec",
    "FindingAnchor",
    "FindingDraftList",
    "HealthStatus",
    "IncrementalReviewContext",
    "IncrementalReviewResult",
    "Invocation",
    "InvocationMode",
    "InvocationStatus",
    "InvocationTelemetry",
    "OnActivity",
    "PluginNotFoundError",
    "PriorThreadMessage",
    "ReportedFinding",
    "ReviewContext",
    "ReviewResult",
    "Severity",
    "StaleCheckContext",
    "StaleCheckDto",
    "StaleCheckResult",
    "Usage",
    "ValidationResult",
    "VerifyFixContext",
    "VerifyFixDto",
    "VerifyFixResult",
    "answer_question",
    "assemble_answer_question_prompt",
    "assemble_incremental_review_prompt",
    "assemble_stale_check_prompt",
    "assemble_verify_fix_prompt",
    "bind_coding_agent_registry",
    "build_invocation",
    "create_run",
    "current_coding_agent_registry",
    "finalize_run",
    "finding_output_schema",
    "get_plugin",
    "get_run_id_for_command",
    "get_run_id_for_workflow_step",
    "get_step_activity",
    "health_check_all",
    "incremental_review",
    "list_registered_plugins",
    "register_coding_agent_plugin",
    "register_plugin",
    "registered_plugin_ids",
    "review",
    "schema_appendix",
    "stale_check",
    "validate_config",
    "verify_fix",
]
