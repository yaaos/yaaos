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
- `dispatch_provision_workspace(org_id, workspace_id, *, ..., session)` helper
  that enqueues a ProvisionWorkspace command durably inside the caller's transaction.
- `dispatch_cleanup_workspace(workspace_id, *, org_id, agent_id, traceparent, session)`
  that enqueues a CleanupWorkspace command pinned to the owning agent.
- `provision()` / `destroy()` that hand control to the agent via
  `ProvisionWorkspace` / `CleanupWorkspace` AgentCommands.

The synchronous-shaped Workspace Protocol methods (`run_coding_agent_cli`
returning a `CodingAgentCliResult`) don't fit the async event-driven
model — the reviewer commands enqueue AgentCommands that the engine's
`handle_agent_event` consumes instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid7

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    AuthBlock,
    CleanupWorkspaceCommand,
    ProvisionWorkspaceCommand,
    RepoRef,
    enqueue_command,
    has_any_reachable_agent,
    pin_command_to_agent,
)
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

    plugin_id = "remote_agent"

    async def provision(self, spec: WorkspaceSpec) -> dict[str, Any]:
        # Provisioning runs through the dispatch helpers, not this
        # synchronous Protocol method: they enqueue a `ProvisionWorkspace`
        # AgentCommand and the workflow engine awaits the
        # `completed_success` event.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.provision is not a synchronous call; "
            "use dispatch_provision_workspace() to enqueue commands."
        )

    async def run_coding_agent_cli(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
        on_stream_line: OnStreamLine | None = None,
    ) -> CodingAgentCliResult:
        del argv, env, stdin, timeout_seconds, on_stream_line
        # The reviewer commands enqueue `InvokeClaudeCode` AgentCommands
        # and the workflow engine awaits the terminal event. Calling this
        # method directly is a programming error against the remote provider.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider has no synchronous run_coding_agent_cli; "
            "Workspace WorkflowCommands enqueue AgentCommands and await events."
        )

    async def read_text(self, path: str) -> str | None:
        del path
        # Same shape issue as run_coding_agent_cli — reads come back as
        # outputs on terminal events in the remote model.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.read_text is not a synchronous call; "
            "reads arrive as outputs on terminal AgentEvents."
        )

    async def write_text(self, path: str, content: str) -> None:
        del path, content
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.write_text is not a synchronous call; "
            "use a `WriteFiles` AgentCommand."
        )

    async def destroy(self) -> None:
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


class ProvisionWorkspaceDispatch(BaseModel):
    """Result of `dispatch_provision_workspace`.

    Carries the new `command_id` for `current_command_id`. The workspace
    is not pre-assigned to an agent — the durable queue lets any reachable
    agent claim it via `claim_next`.
    """

    model_config = ConfigDict(frozen=True)
    command_id: UUID


async def dispatch_provision_workspace(
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
    workflow_execution_id: UUID | None = None,
) -> ProvisionWorkspaceDispatch:
    """Enqueue a `ProvisionWorkspace` command durably inside the caller's transaction.

    Returns a `ProvisionWorkspaceDispatch` (the new `command_id`) so the caller
    can persist `current_command_id` atomically with the `try_claim` gate.

    There is no agent pre-assignment here — the durable queue lets whichever
    reachable agent has capacity claim it via `claim_next`.

    Caller is responsible for calling `core/workspace.try_claim` to gate
    the dispatch through the single-flight machinery; this helper does NOT
    write to the workspace row itself.

    `workflow_execution_id` is stamped on the new `agent_commands` row so the
    terminal-event ingestion path can resolve `command_id → workflow` directly,
    without a workspace-row lookup. Defaults to NULL for non-workflow callers.
    """
    command_id = uuid7()
    cmd = ProvisionWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent=traceparent,
        repo=repo,
        history=history,
        auth=auth,
        ttl_seconds=ttl_seconds,
        max_idle_seconds=max_idle_seconds,
    )
    await enqueue_command(
        org_id=org_id,
        command=cmd,
        session=session,
        workflow_execution_id=workflow_execution_id,
    )
    return ProvisionWorkspaceDispatch(command_id=command_id)


async def dispatch_invoke_claude_code(
    workspace_id: UUID,
    *,
    org_id: UUID,
    agent_id: UUID,
    invocation: dict,  # type: ignore[type-arg]
    traceparent: str,
    session: AsyncSession,
    workflow_execution_id: UUID | None = None,
) -> UUID:
    """Enqueue an `InvokeClaudeCode` command pinned to the owning agent.

    `agent_id` is the workspace's `owning_agent_id` — the pod that ran
    `ProvisionWorkspace`. Post-provision commands MUST route to that same
    agent because only that pod has the checkout. The command is pinned
    via `pin_command_to_agent` so `claim_next`'s workspace_ids sweep finds it.
    Returns the new `command_id`.

    `invocation` is the serialised `Invocation` value object from
    `domain/coding_agent.build_review_invocation`; it carries the skill
    handle, argv/stdin/env exec spec, and per-run limits.
    """
    from app.core.agent_gateway import InvokeClaudeCodeCommand, InvokeClaudeCodeLimits  # noqa: PLC0415

    command_id = uuid7()
    limits_raw = invocation.get("limits") or {}
    limits = InvokeClaudeCodeLimits(wallclock_seconds=limits_raw.get("wallclock_seconds", 1200))
    cmd = InvokeClaudeCodeCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent=traceparent,
        invocation=invocation,
        limits=limits,
        mcp_servers=(),
    )
    await enqueue_command(
        org_id=org_id,
        command=cmd,
        session=session,
        workflow_execution_id=workflow_execution_id,
    )
    await pin_command_to_agent(command_id, agent_id, session=session)
    return command_id


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
    — the pod that ran `ProvisionWorkspace`. Post-provision commands MUST go to
    that same agent; re-picking would route to a pod that has no such workspace.
    The command row is pre-stamped with `agent_id` so `claim_next` can
    find it in the workspace_ids sweep.
    Returns the new `command_id`.
    """
    command_id = uuid7()
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent=traceparent,
    )
    await enqueue_command(org_id=org_id, command=cmd, session=session)
    # Pre-assign the agent so claim_next's workspace_ids sweep finds it.
    await pin_command_to_agent(command_id, agent_id, session=session)
    return command_id


def register_workspace_providers() -> None:
    """Register the shipped workspace provider into the process registry.

    Called explicitly from the web + worker composition roots after the
    workspace module is loaded — not at import time, so the process controls
    when registration happens (mirrors `register_workspace_recovery_policies`).
    `RemoteAgentWorkspaceProvider` is the only shipped implementation: it
    dispatches every workspace operation to a customer-deployed WorkspaceAgent
    via `core/agent_gateway`. `ProvisionWorkspace.dispatch` requires at least
    one registered provider, so this call is load-bearing for the review +
    enumerate workflows. Called exactly once per process; the registry raises
    loudly on a duplicate, which is the intended signal for a wiring bug."""
    from app.core.workspace.service import register_workspace_provider  # noqa: PLC0415

    register_workspace_provider(RemoteAgentWorkspaceProvider())
