"""core/coding_agent — Protocol + registry for coding-agent CLI plugins.

The Protocol exposes two pure methods: `build_invocation` translates a
high-level `Invocation` (skill, model, effort, context, wallclock cap) into
a concrete `InvokeCodingAgent` exec block; `parse_result` decodes a terminal
AgentEvent payload into a `RunResult`. Plugins own skill resolution, model
mapping, and stdout parsing. `dispatch_invocation` enqueues the exec block
as an `InvokeClaudeCode` AgentCommand, inserts a run row, and pins the
command to the owning agent.
"""

from app.core.agent_gateway import register_run_sink as _register_run_sink

# Import the partition-maintenance module for its `@scheduled` side effect —
# registers the daily `coding_agent_activity_partition_maintenance` task with
# the broker + scheduler registry at import time.
from app.core.coding_agent import partition_maintenance as _partition_maintenance  # noqa: F401
from app.core.coding_agent.run_service import (
    create_run,
    get_step_activity,
)
from app.core.coding_agent.run_sink_impl import CodingAgentRunSinkImpl
from app.core.coding_agent.service import (
    CodingAgentRegistry,
    bind_coding_agent_registry,
    current_coding_agent_registry,
    dispatch_invocation,
    get_plugin,
    register_plugin,
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

__all__ = [
    "ACTIVITY_EVENT_KINDS",
    "ActivityEvent",
    "ActivityLog",
    "CodingAgentError",
    "CodingAgentPlugin",
    "CodingAgentRegistry",
    "Effort",
    "Invocation",
    "InvokeCodingAgent",
    "PluginNotFoundError",
    "RunResult",
    "RunStatus",
    "Usage",
    "bind_coding_agent_registry",
    "create_run",
    "current_coding_agent_registry",
    "dispatch_invocation",
    "get_plugin",
    "get_step_activity",
    "register_plugin",
]
