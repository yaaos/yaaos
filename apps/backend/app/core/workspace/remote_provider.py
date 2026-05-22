"""`RemoteAgentWorkspaceProvider` — dispatches to a customer-deployed
WorkspaceAgent via `core/agent_gateway`.

This provider does **not** spawn anything in-process. Each operation
(`provision`, `run_coding_agent_cli`, `destroy`, etc.) enqueues an
AgentCommand onto the target agent's FIFO via
`core/agent_gateway.enqueue_command`. The Phase 1 workflow engine's
Workspace branch already parks in `awaiting_agent` after dispatch; the
terminal AgentEvent arrives at `/api/v1/commands/{id}/events` and the
engine's `handle_agent_event` resumes the workflow.

Phase 7 foundations ships:
- Provider registration under id `remote_agent`.
- `dispatch_to_agent(workspace_id, command, *, session)` helper that
  picks the destination agent (least-loaded reachable for the workspace's
  org) and enqueues the command.
- Placeholder `provision()` / `destroy()` that hand control to the agent
  via `CreateWorkspace` / `CleanupWorkspace` AgentCommands.

Deferred to the Phase 7 follow-on:
- Synchronous-shaped Workspace Protocol methods (`run_coding_agent_cli`
  returning a `CodingAgentCliResult`) become awkward in the async
  event-driven model — the full integration replaces these with the
  Phase 4 reviewer commands that consume the engine's `handle_agent_event`.
- Actual provisioning policy beyond "first reachable agent".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import (
    AuthBlock,
    CleanupWorkspaceCommand,
    CreateWorkspaceCommand,
    RepoRef,
    enqueue_command,
)
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.plugin_meta import PluginMeta
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
    Phase 7 foundations exposes the dispatch entry points; Phase 7
    follow-on wires the full integration into the reviewer workflows
    (Phase 4 command bodies)."""

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
        # Phase 7 follow-on wires real provisioning. The full
        # implementation enqueues a `CreateWorkspace` AgentCommand and
        # returns plugin_state pointing at the workspace id; the workflow
        # engine awaits the `completed_success` event.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.provision is wired in Phase 7 follow-on; "
            "use dispatch_to_agent() to enqueue commands today."
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
        # The Phase 4 reviewer commands replace this entirely — they
        # enqueue `InvokeClaudeCode` AgentCommands and the workflow engine
        # awaits the terminal event. Calling this method directly is a
        # programming error against the remote provider.
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider has no synchronous run_coding_agent_cli; "
            "Workspace WorkflowCommands enqueue AgentCommands and await events."
        )

    async def read_text(self, plugin_state: dict[str, Any], path: str) -> str | None:
        del plugin_state, path
        # Same shape issue as run_coding_agent_cli — reads come back as
        # outputs on terminal events in the remote model.
        raise WorkspaceProvisionError("RemoteAgentWorkspaceProvider.read_text is wired in Phase 7 follow-on.")

    async def write_text(self, plugin_state: dict[str, Any], path: str, content: str) -> None:
        del plugin_state, path, content
        raise WorkspaceProvisionError(
            "RemoteAgentWorkspaceProvider.write_text is wired in Phase 7 follow-on; "
            "use a `WriteFiles` AgentCommand."
        )

    async def destroy(self, plugin_state: dict[str, Any]) -> None:
        del plugin_state
        # Real implementation enqueues a CleanupWorkspace AgentCommand.
        # The Phase 1 workflow engine's CleanupWorkspace WorkflowCommand
        # is the proper invocation path.
        return

    async def health_check(self) -> HealthStatus:
        """Reachability check — at least one workspace_agents row in the
        deployment has heartbeated within the last 90s (architecture's
        agent-liveness window). Healthy iff at least one reachable pod."""
        # No org context in the Protocol; check globally. Phase 7
        # follow-on wires a per-org variant for the connection-status UI.
        from app.core.database import session as db_session  # noqa: PLC0415

        cutoff = datetime.now(UTC) - timedelta(seconds=90)
        async with db_session() as s:
            rows = (
                (
                    await s.execute(
                        select(WorkspaceAgentRow.id)
                        .where(
                            WorkspaceAgentRow.state == "reachable",
                            WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                            WorkspaceAgentRow.last_heartbeat_at >= cutoff,
                        )
                        .limit(1)
                    )
                )
                .tuples()
                .all()
            )
        healthy = bool(rows)
        return HealthStatus(
            healthy=healthy,
            message="reachable agents present" if healthy else "no reachable agents",
            checked_at=datetime.now(UTC),
        )


# ── Dispatch entry points ──────────────────────────────────────────────


async def pick_agent_for_org(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> WorkspaceAgentRow | None:
    """Pick the least-loaded reachable agent for `org_id`.

    "Load" is the in-process queue depth from
    `core.agent_gateway.queue_depth(agent_id)` — how many AgentCommands
    are waiting for that pod to claim. Among reachable agents (heartbeat
    within the 90-second cutoff), the one with the smallest queue wins;
    tie-break by most-recent heartbeat so a fresh pod beats a stale one
    when both are idle.

    Returns None when no pod is reachable; caller should fail the
    provisioning step with a recoverable error.

    The queue is process-local in the M01 POC. Multi-pod backends will
    swap the load signal for a cross-instance counter (Redis or a
    distributed in_flight_commands count per agent_id) — the policy
    here stays "least loaded → most recent heartbeat", just sourced
    from a shared counter.
    """
    from app.core.agent_gateway import queue_depth  # noqa: PLC0415

    cutoff = datetime.now(UTC) - timedelta(seconds=90)
    rows = (
        (
            await session.execute(
                select(WorkspaceAgentRow)
                .where(
                    WorkspaceAgentRow.org_id == org_id,
                    WorkspaceAgentRow.state == "reachable",
                    WorkspaceAgentRow.last_heartbeat_at.is_not(None),
                    WorkspaceAgentRow.last_heartbeat_at >= cutoff,
                )
                .order_by(WorkspaceAgentRow.last_heartbeat_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    # Sort by (queue_depth ascending, last_heartbeat_at descending).
    # Python's sort is stable, so passing in reverse heartbeat order
    # already lets `min` ties resolve correctly on the secondary key
    # — but be explicit with a tuple so the contract is grep-able.
    return min(
        rows,
        key=lambda r: (queue_depth(r.id), -(r.last_heartbeat_at.timestamp() if r.last_heartbeat_at else 0)),
    )


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
