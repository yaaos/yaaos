"""domain/coding_agent — Protocol + registry for coding-agent CLI plugins.

The Protocol exposes four task modes — `review` (full-review), `incremental_review`
(prev_sha..head only), `verify_fix` (is the finding still present at HEAD?), and
`stale_check` (does the finding still apply after the code changed?). Plugins own
prompt assembly + parsing for each mode; consumers (today: `domain/reviewer`)
hand over domain context and read domain results. Subagent definitions live
under `app/domain/coding_agent/reviewers/` and are installed into the local
Claude Code agent directory by the `plugins/claude_code` plugin at bootstrap.
"""

from app.domain.coding_agent.service import (
    _PLUGINS,
    _reset_plugins_for_tests,
    get_plugin,
    health_check_all,
    incremental_review,
    register_coding_agent_plugin,
    registered_plugin_ids,
    review,
    stale_check,
    validate_config,
    verify_fix,
)
from app.domain.coding_agent.types import (
    ActivityEvent,
    CodingAgentCacheMiss,
    CodingAgentError,
    CodingAgentPlugin,
    FindingAnchor,
    FindingDraft,
    HealthStatus,
    IncrementalReviewContext,
    IncrementalReviewResult,
    InvocationStatus,
    InvocationTelemetry,
    OnActivity,
    PluginNotFoundError,
    ReviewContext,
    ReviewResult,
    Severity,
    StaleCheckContext,
    StaleCheckResult,
    ValidationResult,
    VerifyFixContext,
    VerifyFixResult,
)

__all__ = [
    "_PLUGINS",
    "ActivityEvent",
    "CodingAgentCacheMiss",
    "CodingAgentError",
    "CodingAgentPlugin",
    "FindingAnchor",
    "FindingDraft",
    "HealthStatus",
    "IncrementalReviewContext",
    "IncrementalReviewResult",
    "InvocationStatus",
    "InvocationTelemetry",
    "OnActivity",
    "PluginNotFoundError",
    "ReviewContext",
    "ReviewResult",
    "Severity",
    "StaleCheckContext",
    "StaleCheckResult",
    "ValidationResult",
    "VerifyFixContext",
    "VerifyFixResult",
    "_reset_plugins_for_tests",
    "get_plugin",
    "health_check_all",
    "incremental_review",
    "register_coding_agent_plugin",
    "registered_plugin_ids",
    "review",
    "stale_check",
    "validate_config",
    "verify_fix",
]
