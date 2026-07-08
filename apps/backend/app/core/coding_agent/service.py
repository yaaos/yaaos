"""Registry + dispatch for coding-agent plugins."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Literal
from uuid import UUID

import structlog

from app.core.coding_agent.types import (
    CodingAgentPlugin,
    PluginNotFoundError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.agent_gateway import DispatchContext
    from app.core.coding_agent.types import Invocation

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


def _get() -> CodingAgentRegistry:
    val = _registry_var.get()
    if val is None:
        val = CodingAgentRegistry()
        _registry_var.set(val)
    return val


@contextmanager
def set_coding_agents_for_tests(
    *, scenario: Literal["default", "empty"] = "default"
) -> Iterator[CodingAgentRegistry]:
    """Context manager: bind an isolated registry for the duration.

    ``scenario="default"`` (the default) gives a copy of the current registry,
    preserving production-registered + stub-wrapped plugins. ``scenario="empty"``
    gives a brand-new empty registry — useful for tests that register their own
    plugin set from scratch. Restores the prior binding on exit — even on exception.
    """
    instance = CodingAgentRegistry() if scenario == "empty" else _get().copy()
    token = _registry_var.set(instance)
    try:
        yield instance
    finally:
        _registry_var.reset(token)


def register_plugin(plugin: CodingAgentPlugin) -> None:
    """Register a coding-agent plugin. Raises ValueError if id already taken."""
    _get().register(plugin)


def replace_plugin(plugin: CodingAgentPlugin) -> None:
    """Overwrite-or-insert a plugin in the current registry; used by stub helpers."""
    _get().replace(plugin)


def get_plugin(plugin_id: str) -> CodingAgentPlugin:
    return _get().get(plugin_id)


def list_plugins() -> list[CodingAgentPlugin]:
    """Return all registered coding-agent plugins."""
    return _get().list()


async def dispatch_invocation(
    *,
    invocation: Invocation,
    plugin: CodingAgentPlugin,
    ctx: DispatchContext,
    command_id: UUID,
    session: AsyncSession,
) -> UUID:
    """Build an `InvokeClaudeCode` AgentCommand, dispatch via the workspace
    (Layer 3 → Layer 2 → Layer 1), and insert a run row.

    `command_id` is caller-minted — required, no default. `domain/pipelines`
    mints it before calling this function, since `command_id` also has to
    appear in the skill's `artifact_path`. Minting here would make that
    ordering impossible.

    `workspace_id` is read from `invocation.workspace_id`. Calls
    `plugin.compile_invocation(invocation)` to get the exec block, builds
    an `InvokeClaudeCodeCommand`, and delegates to `dispatch_via_workspace`
    with `claim_workspace=True` — which loads the workspace row (for `org_id`
    + `owning_agent_id`), enqueues, pins to the owning agent, and atomically
    claims. Then inserts a `coding_agent_runs` row. Returns `command_id`.
    Durable iff the caller's transaction commits.

    Raises:
        `CodingAgentError` — `plugin.compile_invocation` failed.
        `WorkspaceNotFoundError` — workspace row absent.
        `WorkspaceClaimFailed` — workspace busy or inactive.
    """
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

    workspace_id = invocation.workspace_id
    # Get org_id from the workspace row — needed for create_run.
    owner = await get_workspace_owner(workspace_id, session=session)
    if owner is None:
        raise WorkspaceNotFoundError(f"workspace {workspace_id} not found")

    invocation_data = plugin.compile_invocation(invocation)

    # Conventional path of the named skill inside the checkout. The agent
    # stats this before spawning claude and fails deterministically
    # (`completed_failure`, reason "skill not found: <path>") when it's
    # absent — zero agent policy, the convention lives here.
    skill_path = f".claude/skills/{invocation.skill}/SKILL.md"

    # Build the typed command. The Go agent reads `invocation.exec.{argv,stdin,env}`;
    # the `exec` wrapper is required — a flat argv dict leaves `inv.Exec.Argv`
    # empty after json.Unmarshal and causes `completed_failure`.
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
        skill_path=skill_path,
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
        workflow_execution_id=ctx.run_id,
        step_id=str(ctx.stage_execution_id),
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
