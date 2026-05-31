"""`RemoteAgentWorkspaceProvider` — dispatches to a customer-deployed
WorkspaceAgent via `core/agent_gateway`.

This provider does **not** spawn anything in-process. Each operation
(`provision`, `run_coding_agent_cli`, `destroy`, etc.) enqueues an
AgentCommand durably in `agent_commands` via `core/agent_gateway.enqueue_command`.
The workflow engine's Workspace branch parks in `awaiting_agent` after dispatch;
the terminal AgentEvent arrives at `/api/v1/commands/{id}/events` and the
engine's `handle_agent_event` resumes the workflow.

Exposes:
- Provider registration under id `remote_agent`.
- `dispatch_create_workspace(org_id, workspace_id, *, ..., session)` helper
  that enqueues a CreateWorkspace command durably inside the caller's transaction.
- `dispatch_cleanup_workspace(workspace_id, *, org_id, agent_id, traceparent, session)`
  that enqueues a CleanupWorkspace command pinned to the owning agent.
- `provision()` / `destroy()` that hand control to the agent via
  `CreateWorkspace` / `CleanupWorkspace` AgentCommands.

The synchronous-shaped Workspace Protocol methods (`run_coding_agent_cli`
returning a `CodingAgentCliResult`) don't fit the async event-driven
model — the reviewer commands enqueue AgentCommands that the engine's
`handle_agent_event` consumes instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    AuthBlock,
    CleanupWorkspaceCommand,
    CreateWorkspaceCommand,
    RepoRef,
    enqueue_command,
    has_any_reachable_agent,
    pin_command_to_agent,
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
    """Implements `WorkspaceProvider`. Registered under id `remote_agent`.

    The Protocol shape was designed for in-process providers — its
    `provision/run/destroy` methods read like a synchronous call/return
    cycle. For the remote path the methods enqueue AgentCommands durably
    and the workflow engine handles awaits via the `handle_agent_event` flow.
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


class CreateWorkspaceDispatch(BaseModel):
    """Result of `dispatch_create_workspace`.

    Carries the new `command_id` for `current_command_id`. The workspace
    is not pre-assigned to an agent — the durable queue lets any reachable
    agent claim it via `claim_batch`.
    """

    model_config = ConfigDict(frozen=True)
    command_id: UUID


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
) -> CreateWorkspaceDispatch:
    """Enqueue a `CreateWorkspace` command durably inside the caller's transaction.

    Returns a `CreateWorkspaceDispatch` (the new `command_id`) so the caller
    can persist `current_command_id` atomically with the `try_claim` gate.

    Unlike the old path there is no agent pre-assignment here — the durable
    queue lets whichever reachable agent has capacity claim it via `claim_batch`.

    Caller is responsible for calling `core/workspace.try_claim` to gate
    the dispatch through the single-flight machinery; this helper does NOT
    write to the workspace row itself.
    """
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
    await enqueue_command(org_id=org_id, command=cmd, session=session)
    return CreateWorkspaceDispatch(command_id=command_id)


async def dispatch_cleanup_workspace(
    workspace_id: UUID,
    *,
    org_id: UUID,
    agent_id: UUID,
    traceparent: str,
    session: AsyncSession,
) -> UUID:
    """Enqueue a `CleanupWorkspace` command pinned to the owning agent.

    `agent_id` is the workspace's stored owning agent (`WorkspaceRow.agent_id`)
    — the pod that ran `CreateWorkspace`. Post-create commands MUST go to that
    same agent; re-picking would route to a pod that has no such workspace.
    The command row is pre-stamped with `agent_id` so `claim_batch` can
    find it in the workspace_ids sweep.
    Returns the new `command_id`.
    """
    command_id = uuid4()
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent=traceparent,
    )
    await enqueue_command(org_id=org_id, command=cmd, session=session)
    # Pre-assign the agent so claim_batch's workspace_ids sweep finds it.
    await pin_command_to_agent(command_id, agent_id, session=session)
    return command_id
