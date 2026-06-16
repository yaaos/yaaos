"""Registry + dispatch for coding-agent plugins."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from app.core.coding_agent.types import (
    CodingAgentPlugin,
    InvokeCodingAgent,
    PluginNotFoundError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    org_id: UUID,
    agent_id: UUID,
    workflow_execution_id: UUID,
    plugin: CodingAgentPlugin,
    invocation_data: InvokeCodingAgent,
    ctx: CommandContext,
    session: AsyncSession,
) -> UUID:
    """Enqueue an `InvokeClaudeCode` AgentCommand and insert a run row.

    Mints a `command_id` (UUIDv7), enqueues the command via
    `core/agent_gateway.enqueue_command`, inserts a `coding_agent_runs`
    row (status=running), and pins the command to `agent_id` so the
    workspace's owning agent will claim it. Returns the minted
    `command_id`. Durable iff the caller's transaction commits.

    `org_id` is required for the agent_commands and coding_agent_runs rows —
    callers source it from their org context (e.g. the workflow execution's
    org, the ticket's org, or the workspace owner's org).

    No workspace-state check. Callers are expected to follow
    `dispatch_invocation` with `try_claim` to enter single-flight mode and to
    surface the `try_claim` failure (which is the single source of truth for
    workspace busy/inactive). Raises `CodingAgentError` on invalid arguments.
    """
    from uuid import uuid7  # noqa: PLC0415

    from app.core.agent_gateway import enqueue_command_payload, pin_command_to_agent  # noqa: PLC0415
    from app.core.coding_agent.run_service import create_run  # noqa: PLC0415

    command_id = uuid7()

    # Build the wire payload from primitives — no agent_gateway typed classes
    # imported here. The Go agent reads `invocation.exec.{argv,stdin,env}`;
    # the top-level `exec` wrapper is required — a flat `{argv,stdin,env}` dict
    # leaves `inv.Exec.Argv` empty after json.Unmarshal and causes
    # `completed_failure` with "invocation.exec.argv missing or empty".
    # `traceparent` is "" when no parent span is active; `enqueue_command_payload`
    # overwrites it with the dispatch span's traceparent before persisting.
    await enqueue_command_payload(
        org_id,
        command_id=command_id,
        kind="InvokeClaudeCode",
        workspace_id=workspace_id,
        payload={
            "invocation": {
                "exec": {
                    "argv": invocation_data.argv,
                    "stdin": invocation_data.stdin or "",
                    "env": dict(invocation_data.env),
                }
            },
            "mcp_servers": [],
            "limits": {"wallclock_seconds": invocation_data.wallclock_seconds},
            "result_spec": {},
        },
        traceparent=ctx.traceparent or "",
        session=session,
        workflow_execution_id=workflow_execution_id,
    )

    await create_run(
        org_id=org_id,
        workflow_execution_id=workflow_execution_id,
        step_id=ctx.step_id,
        agent_command_id=command_id,
        command_kind="InvokeClaudeCode",
        plugin_id=plugin.plugin_id,
        session=session,
    )

    await pin_command_to_agent(command_id, agent_id, session=session)

    log.info(
        "coding_agent.dispatch_invocation",
        command_id=str(command_id),
        workspace_id=str(workspace_id),
        agent_id=str(agent_id),
        plugin_id=plugin.plugin_id,
        wallclock_seconds=invocation_data.wallclock_seconds,
    )
    return command_id
