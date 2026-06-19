"""Registry + dispatch for coding-agent plugins."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from app.core.coding_agent.types import (
    CodingAgentPlugin,
    PluginNotFoundError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.coding_agent.types import Invocation
    from app.core.workflow.types import CommandContext

log = structlog.get_logger("coding_agent")


class CodingAgentRegistry:
    """Coding-agent plugin map. ContextVar-bound so each test context gets a
    fresh, isolated instance; production rides the import-time default for the
    process lifetime — it never calls bind_coding_agent_registry(). The
    ContextVar exists solely for per-test isolation (see app/testing/isolation.py)."""

    def __init__(self) -> None:
        self._plugins: dict[str, CodingAgentPlugin] = {}

    def register(self, plugin: CodingAgentPlugin) -> None:
        if plugin.plugin_id in self._plugins:
            raise ValueError(f"coding agent plugin {plugin.plugin_id!r} already registered")
        self._plugins[plugin.plugin_id] = plugin

    def replace(self, plugin: CodingAgentPlugin) -> None:
        """Overwrite-or-insert; used by stub/fake helpers."""
        self._plugins[plugin.plugin_id] = plugin

    def get(self, plugin_id: str) -> CodingAgentPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as e:
            raise PluginNotFoundError(plugin_id) from e

    def list(self) -> list[CodingAgentPlugin]:
        return list(self._plugins.values())

    def items(self) -> tuple[tuple[str, CodingAgentPlugin], ...]:
        """Return a snapshot of (plugin_id, plugin) pairs.

        Returns a tuple so callers cannot mutate registry state through the
        returned collection.
        """
        return tuple(self._plugins.items())

    def ids(self) -> list[str]:
        return list(self._plugins.keys())

    def copy(self) -> CodingAgentRegistry:
        clone = CodingAgentRegistry()
        clone._plugins = dict(self._plugins)
        return clone


_registry_var: ContextVar[CodingAgentRegistry | None] = ContextVar("_coding_agent_registry_var", default=None)
# Import-time default: plugins that call register_plugin() at module-import
# time (bootstrap()) land here when no per-test binding is active. Production
# never calls bind_coding_agent_registry(); the ContextVar exists solely for
# per-test isolation.
_default_registry = CodingAgentRegistry()


def bind_coding_agent_registry(instance: CodingAgentRegistry) -> None:
    _registry_var.set(instance)


def current_coding_agent_registry() -> CodingAgentRegistry:
    return _registry_var.get() or _default_registry


def register_plugin(plugin: CodingAgentPlugin) -> None:
    """Register a coding-agent plugin. Raises ValueError if id already taken."""
    current_coding_agent_registry().register(plugin)


def get_plugin(plugin_id: str) -> CodingAgentPlugin:
    return current_coding_agent_registry().get(plugin_id)


async def dispatch_invocation(
    *,
    workspace_id: UUID,
    invocation: Invocation,
    plugin: CodingAgentPlugin,
    ctx: CommandContext,
    session: AsyncSession,
) -> UUID:
    """Build an `InvokeClaudeCode` AgentCommand, dispatch via the workspace
    (Layer 3 → Layer 2 → Layer 1), and insert a run row.

    Calls `plugin.build_invocation(invocation)` to get the exec block, builds
    an `InvokeClaudeCodeCommand`, and delegates to `dispatch_via_workspace`
    with `claim_workspace=True` — which loads the workspace row (for `org_id`
    + `owning_agent_id`), enqueues, pins to the owning agent, and atomically
    claims. Then inserts a `coding_agent_runs` row. Returns the minted
    `command_id`. Durable iff the caller's transaction commits.

    Raises:
        `CodingAgentError` — `plugin.build_invocation` failed.
        `WorkspaceNotFoundError` — workspace row absent.
        `WorkspaceClaimFailed` — workspace busy or inactive.
    """
    from uuid import uuid7  # noqa: PLC0415

    from app.core.agent_gateway import (  # noqa: PLC0415
        InvokeClaudeCodeCommand,
        InvokeClaudeCodeLimits,
    )
    from app.core.coding_agent.run_service import create_run  # noqa: PLC0415
    from app.core.workspace import (  # noqa: PLC0415
        WorkspaceNotFoundError,
        dispatch_via_workspace,
        get_workspace_owner,
    )

    # Get org_id from the workspace row — needed for create_run.
    owner = await get_workspace_owner(workspace_id, session=session)
    if owner is None:
        raise WorkspaceNotFoundError(f"workspace {workspace_id} not found")

    invocation_data = plugin.build_invocation(invocation)

    # Build the typed command. The Go agent reads `invocation.exec.{argv,stdin,env}`;
    # the `exec` wrapper is required — a flat argv dict leaves `inv.Exec.Argv`
    # empty after json.Unmarshal and causes `completed_failure`.
    command_id = uuid7()
    cmd = InvokeClaudeCodeCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent=ctx.traceparent or "",
        invocation={
            "exec": {
                "argv": invocation_data.argv,
                "stdin": invocation_data.stdin or "",
                "env": dict(invocation_data.env),
            }
        },
        mcp_servers=(),
        limits=InvokeClaudeCodeLimits(wallclock_seconds=invocation_data.wallclock_seconds),
        result_spec={},
    )

    # Layer 2: enqueue + pin + claim atomically.
    await dispatch_via_workspace(
        command=cmd,
        workspace_id=workspace_id,
        ctx=ctx,
        session=session,
        claim_workspace=True,
    )

    await create_run(
        org_id=owner.org_id,
        workflow_execution_id=UUID(ctx.workflow_execution_id),
        step_id=ctx.step_id,
        agent_command_id=command_id,
        command_kind="InvokeClaudeCode",
        plugin_id=plugin.plugin_id,
        session=session,
    )

    log.info(
        "coding_agent.dispatch_invocation",
        command_id=str(command_id),
        workspace_id=str(workspace_id),
        org_id=str(owner.org_id),
        plugin_id=plugin.plugin_id,
        wallclock_seconds=invocation_data.wallclock_seconds,
    )
    return command_id
