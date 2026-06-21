"""core/coding_agent — Protocol + registry for coding-agent CLI plugins.

The Plugin Protocol exposes two pure methods: `compile_invocation` translates a
high-level `Invocation` (skill, model, effort, context, wallclock cap) into
a concrete `InvokeCodingAgent` exec block; `parse_result` decodes a terminal
AgentEvent payload into a `RunResult`. Plugins own skill resolution, model
mapping, and stdout parsing. `dispatch_invocation` (Layer 3) calls
`plugin.compile_invocation`, builds an `InvokeClaudeCodeCommand`, delegates to
`dispatch_via_workspace` (Layer 2) with `claim_workspace=True` for the atomic
enqueue + pin + claim, then inserts a `coding_agent_runs` row.

`CodingAgentCommand` is the abstract base for workflow commands that invoke a
coding-agent plugin; concrete impls live in `domain/<consumer>/commands/`.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    enqueue_config_update_for_all_org_agents as _enqueue_config_update_for_all_org_agents,
)
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
from app.core.coding_agent.commands_base import CodingAgentCommand
from app.core.coding_agent.run_service import (
    create_run,
    get_step_activity,
)
from app.core.coding_agent.run_sink_impl import CodingAgentRunSinkImpl
from app.core.coding_agent.service import _get as _get_coding_agent_registry
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


async def build_byok_secrets_for_org(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> dict[str, SecretStr]:
    """Return a dict of provider_id → SecretStr for every registered plugin
    that has a byok_requirement and a stored key for the org.

    Called by `core/agent_gateway._build_config_update_dto` via the registered
    byok-secrets-provider IoC seam. Reads BYOK keys for all plugins that declare
    a `byok_requirement()`. Wraps each plaintext in SecretStr immediately so it
    never crosses module boundaries as a bare string.
    """
    import app.core.byok as _byok  # noqa: PLC0415

    result: dict[str, SecretStr] = {}
    for plugin in _get_coding_agent_registry().list():
        req = plugin.byok_requirement()
        if req is None:
            continue
        plaintext = await _byok.get(org_id, req, session=session)
        if plaintext is not None:
            result[req] = SecretStr(plaintext)
    return result


_register_byok_secrets_provider(build_byok_secrets_for_org)


def _register_byok_on_change() -> None:
    """Register the ConfigUpdate fan-out callback with byok at import time.

    In-function import keeps the `core/byok` edge out of the top-level namespace
    (tach `depends_on` lists top-level imports). The import is a module-level
    side-effect, executed once at first import of `core/coding_agent`.
    """
    import app.core.byok as _byok  # noqa: PLC0415

    _byok.register_on_change(_enqueue_config_update_for_all_org_agents)


_register_byok_on_change()

__all__ = [
    "ACTIVITY_EVENT_KINDS",
    "ActivityEvent",
    "ActivityLog",
    "CodingAgentCommand",
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
    "get_plugin",
    "get_step_activity",
    "list_plugins",
    "register_plugin",
    "replace_plugin",
    "set_coding_agents_for_tests",
]
