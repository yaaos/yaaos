"""core/coding_agent — Protocol + registry for coding-agent CLI plugins and per-org installs.

The Plugin Protocol exposes two pure methods: `compile_invocation` translates a
high-level `Invocation` (skill, model, effort, context, wallclock cap) into
a concrete `InvokeCodingAgent` exec block; `parse_result` decodes a terminal
AgentEvent payload into a `RunResult`. Plugins own skill resolution, model
mapping, and stdout parsing. `dispatch_invocation` (Layer 3) calls
`plugin.compile_invocation`, builds an `InvokeClaudeCodeCommand`, delegates to
`dispatch_via_workspace` (Layer 2) with `claim_workspace=True` for the atomic
enqueue + pin + claim, then inserts a `coding_agent_runs` row.

Per-org install state (`org_coding_agents` table) lives here too — each module
stores its own settings.
"""

from __future__ import annotations

from app.core.agent_gateway import (
    register_api_key_secrets_provider as _register_api_key_secrets_provider,
)
from app.core.agent_gateway import (
    register_command_hydrator as _register_command_hydrator,
)
from app.core.agent_gateway import (
    register_run_sink as _register_run_sink,
)

# Import the partition-maintenance module for its `@scheduled` side effect —
# registers the daily `coding_agent_activity_partition_maintenance` task with
# the broker + scheduler registry at import time.
from app.core.coding_agent import partition_maintenance as _partition_maintenance  # noqa: F401
from app.core.coding_agent.api_keys import (
    _config_update_hydrator,
    _register_api_key_on_change,
    build_api_key_secrets_for_org,
)
from app.core.coding_agent.installs import (
    CodingAgentAlreadyInstalledError,
    CodingAgentInstall,
    CodingAgentNotInstalledError,
    install_coding_agent,
    list_coding_agents,
    uninstall_coding_agent,
    update_coding_agent_settings,
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
from app.core.coding_agent.skills_bundle import build_skills_bundle_zip
from app.core.coding_agent.types import (
    ACTIVITY_EVENT_KINDS,
    ActivityEvent,
    ActivityLog,
    AgentSource,
    BundleFile,
    CodingAgentError,
    CodingAgentPlugin,
    Effort,
    Invocation,
    InvokeCodingAgent,
    PluginNotFoundError,
    RunResult,
    RunStatus,
    SkillSource,
    StageOptions,
    Usage,
)

_register_run_sink(CodingAgentRunSinkImpl())
_register_api_key_secrets_provider(build_api_key_secrets_for_org)
_register_api_key_on_change()
_register_command_hydrator("ConfigUpdate", _config_update_hydrator)

__all__ = [
    "ACTIVITY_EVENT_KINDS",
    "ActivityEvent",
    "ActivityLog",
    "AgentSource",
    "BundleFile",
    "CodingAgentAlreadyInstalledError",
    "CodingAgentError",
    "CodingAgentInstall",
    "CodingAgentNotInstalledError",
    "CodingAgentPlugin",
    "Effort",
    "Invocation",
    "InvokeCodingAgent",
    "PluginNotFoundError",
    "RunResult",
    "RunStatus",
    "SkillSource",
    "StageOptions",
    "Usage",
    "build_api_key_secrets_for_org",
    "build_skills_bundle_zip",
    "create_run",
    "dispatch_invocation",
    "finalize_run",
    "get_plugin",
    "get_stage_activity",
    "install_coding_agent",
    "list_coding_agents",
    "list_plugins",
    "register_plugin",
    "replace_plugin",
    "set_coding_agents_for_tests",
    "uninstall_coding_agent",
    "update_coding_agent_settings",
]

# Side-effect import: load route-registering web submodule at package import
# time so callers need only `import app.core.coding_agent`. Not in __all__ (Rule-9).
import app.core.coding_agent.installs_web  # noqa: E402, F401
