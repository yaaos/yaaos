"""`RemoteAgentWorkspaceProvider` — dispatches to a customer-deployed
WorkspaceAgent via `core/agent_gateway`.

This provider does **not** spawn anything in-process. Each operation
(`provision`, `run_coding_agent_cli`, `destroy`, etc.) enqueues an
AgentCommand onto the target agent's FIFO via
`core/agent_gateway.enqueue_command`. The workflow engine's Workspace
branch parks in `awaiting_agent` after dispatch; the terminal AgentEvent
arrives at `/api/v1/commands/{id}/events` and the engine's
`handle_agent_event` resumes the workflow.

Exposes:
- Provider registration under id `remote_agent`.
- `dispatch_to_agent(workspace_id, command, *, session)` helper that
  picks the destination agent (least-loaded reachable for the workspace's
  org) and enqueues the command.
- `provision()` / `destroy()` that hand control to the agent via
  `CreateWorkspace` / `CleanupWorkspace` AgentCommands.

The synchronous-shaped Workspace Protocol methods (`run_coding_agent_cli`
returning a `CodingAgentCliResult`) don't fit the async event-driven
model — the reviewer commands enqueue AgentCommands that the engine's
`handle_agent_event` consumes instead. Provisioning policy is "first
reachable agent".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    AuthBlock,
    CleanupWorkspaceCommand,
    CreateWorkspaceCommand,
    RepoRef,
    enqueue_command,
    has_any_reachable_agent,
    pick_agent_for_org,
)
from app.core.plugin_kit import PluginMeta
from app.core.workspace.types import (
    CodingAgentCliResult,
    HealthStatus,
    OnStreamLine,
    WorkspaceProvisionError,
    WorkspaceSpec,
)

log = structlog.get_logger("core.workspace.remote_provider")


class RemoteAgentWorkspaceProvider:
    """Implements `WorkspaceProvider`. Persisted under
    `workspaces.provider = 'remote_agent'`.

    The Protocol shape was designed for in-process providers — its
    `provision/run/destroy` methods read like a synchronous call/return
    cycle. For the remote path the methods enqueue AgentCommands and the
    workflow engine handles awaits via the `handle_agent_event` flow.
    The dispatch entry points enqueue commands; the reviewer workflows
    drive the full integration through their command bodies."""

    meta = PluginMeta(
        id="remote_agent",
        type="workspace",
        display_name="Remote agent",
        description=(
            "Dispatch workspaces to a customer-deployed WorkspaceAgent. The "
            "agent process spawns the workspace and runs coding-agent CLIs "
            "locally; only metadata and findings cross the trust boundary."
        ),
        docs_url="https://github.com/yaaos/yaaos/blob/main/docs/system-architecture.md",
    )

    async def provision(self, spec: WorkspaceSpec) -> dict[str, Any]:
        # Provisioning runs through the dispatch helpers, not this
        # synchronous Protocol method: they enqueue a `CreateWorkspace`
        # AgentCommand and the workflow engine awaits the
        # `completed_success` event.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.provision is not a synchronous call; "
            "use dispatch_create_workspace() to enqueue commands."
        )

    async def run_coding_agent_cli(
        self,
        plugin_state: dict[str, Any],
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
        on_stream_line: OnStreamLine | None = None,
    ) -> CodingAgentCliResult:
        del plugin_state, argv, env, stdin, timeout_seconds, on_stream_line
        # The reviewer commands enqueue `InvokeClaudeCode` AgentCommands
        # and the workflow engine awaits the terminal event. Calling this
        # method directly is a programming error against the remote provider.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider has no synchronous run_coding_agent_cli; "
            "Workspace WorkflowCommands enqueue AgentCommands and await events."
        )

    async def read_text(self, plugin_state: dict[str, Any], path: str) -> str | None:
        del plugin_state, path
        # Same shape issue as run_coding_agent_cli — reads come back as
        # outputs on terminal events in the remote model.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.read_text is not a synchronous call; "
            "reads arrive as outputs on terminal AgentEvents."
        )

    async def write_text(self, plugin_state: dict[str, Any], path: str, content: str) -> None:
        del plugin_state, path, content
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.write_text is not a synchronous call; "
            "use a `WriteFiles` AgentCommand."
        )

    async def destroy(self, plugin_state: dict[str, Any]) -> None:
        del plugin_state
        # Destruction runs through the engine's CleanupWorkspace
        # WorkflowCommand, which enqueues a CleanupWorkspace AgentCommand;
        # that is the proper invocation path, not this method.
        return

    async def health_check(self) -> HealthStatus:
        """Reachability check — at least one workspace_agents row in the
        deployment has heartbeated within the last 90s (architecture's
        agent-liveness window). Healthy iff at least one reachable pod."""
        # No org context in the Protocol; check globally.
        from app.core.database import session as db_session  # noqa: PLC0415

        async with db_session() as s:
            healthy = await has_any_reachable_agent(session=s)
        return HealthStatus(
            healthy=healthy,
            message="reachable agents present" if healthy else "no reachable agents",
            checked_at=datetime.now(UTC),
        )


# ── Dispatch entry points ──────────────────────────────────────────────


async def dispatch_create_workspace(
    org_id: UUID,
    workspace_id: UUID,
    *,
    repo: RepoRef,
    auth: AuthBlock,
    traceparent: str,
    history: int = 1,
    ttl_seconds: int = 600,
    max_idle_seconds: int = 600,
    session: AsyncSession,
) -> UUID | None:
    """Pick an agent for `org_id` and enqueue a `CreateWorkspace` command.
    Returns the new `command_id` so the caller can store it on the workspace
    row (`current_command_id`), or None when no agent is reachable.

    Caller is responsible for calling `core/workspace.try_claim` to gate
    the dispatch through the single-flight machinery; this helper does NOT
    write to the workspace row itself."""
    agent = await pick_agent_for_org(org_id, session=session)
    if agent is None:
        log.warning("remote_provider.no_reachable_agent", org_id=str(org_id))
        return None
    command_id = uuid4()
    cmd = CreateWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent=traceparent,
        repo=repo,
        history=history,
        auth=auth,
        ttl_seconds=ttl_seconds,
        max_idle_seconds=max_idle_seconds,
    )
    await enqueue_command(agent.agent_pod_id, cmd)
    return command_id


async def dispatch_cleanup_workspace(
    org_id: UUID,
    workspace_id: UUID,
    *,
    traceparent: str,
    session: AsyncSession,
) -> UUID | None:
    """Enqueue a `CleanupWorkspace` against the agent that owns the workspace."""
    agent = await pick_agent_for_org(org_id, session=session)
    if agent is None:
        return None
    command_id = uuid4()
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent=traceparent,
    )
    await enqueue_command(agent.agent_pod_id, cmd)
    return command_id
