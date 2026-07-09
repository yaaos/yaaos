"""core/coding_agent — Protocol + registry for coding-agent CLI plugins.

The Plugin Protocol exposes two pure methods: `compile_invocation` translates a
high-level `Invocation` (skill, model, effort, context, wallclock cap) into
a concrete `InvokeCodingAgent` exec block; `parse_result` decodes a terminal
AgentEvent payload into a `RunResult`. Plugins own skill resolution, model
mapping, and stdout parsing. `dispatch_invocation` (Layer 3) calls
`plugin.compile_invocation`, builds an `InvokeClaudeCodeCommand`, delegates to
`dispatch_via_workspace` (Layer 2) with `claim_workspace=True` for the atomic
enqueue + pin + claim, then inserts a `coding_agent_runs` row.
"""

from __future__ import annotations

from app.core.agent_gateway import (
    register_byok_secrets_provider as _register_byok_secrets_provider,
)
from app.core.agent_gateway import (
    register_run_sink as _register_run_sink,
)

# Import the partition-maintenance module for its `@scheduled` side effect —
# registers the daily `coding_agent_activity_partition_maintenance` task with
# the broker + scheduler registry at import time.
from app.core.coding_agent import partition_maintenance as _partition_maintenance  # noqa: F401
from app.core.coding_agent.byok import (
    _register_byok_on_change,
    build_byok_secrets_for_org,
)
from app.core.coding_agent.run_service import (
    create_run,
    finalize_run,
    get_stage_activity,
)
from app.core.coding_agent.run_sink_impl import CodingAgentRunSinkImpl
from app.core.coding_agent.service import (
    dispatch_invocation,
    get_plugin,
    list_plugins,
    register_plugin,
    replace_plugin,
    set_coding_agents_for_tests,
)
from app.core.coding_agent.types import (
    ACTIVITY_EVENT_KINDS,
    ActivityEvent,
    ActivityLog,
    CodingAgentError,
    CodingAgentPlugin,
    Effort,
    Invocation,
    InvokeCodingAgent,
    PluginNotFoundError,
    RunResult,
    RunStatus,
    Usage,
)

_register_run_sink(CodingAgentRunSinkImpl())
_register_byok_secrets_provider(build_byok_secrets_for_org)
_register_byok_on_change()

__all__ = [
    "ACTIVITY_EVENT_KINDS",
    "ActivityEvent",
    "ActivityLog",
    "CodingAgentError",
    "CodingAgentPlugin",
    "Effort",
    "Invocation",
    "InvokeCodingAgent",
    "PluginNotFoundError",
    "RunResult",
    "RunStatus",
    "Usage",
    "build_byok_secrets_for_org",
    "create_run",
    "dispatch_invocation",
    "finalize_run",
    "get_plugin",
    "get_stage_activity",
    "list_plugins",
    "register_plugin",
    "replace_plugin",
    "set_coding_agents_for_tests",
]
