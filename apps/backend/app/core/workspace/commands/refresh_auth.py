"""RefreshWorkspaceAuth — auth-refresh AgentDispatchCommand.

Recovery command declared in `pr_review_v1.recovery_commands`. Bound to
`auth_expired` failure labels via its `recovers_failure_label` class attribute.
The engine inserts this command before re-dispatching the originally-failing
AgentCommand so the Go agent can rotate its checkout's auth header before
the retry.

`build_command` returns a placeholder `CleanupWorkspaceCommand` — a real
`RefreshWorkspaceAuth` AgentCommand wire type is deferred.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid7

import structlog
from pydantic import BaseModel, ConfigDict

from app.core.agent_gateway import CleanupWorkspaceCommand
from app.core.workflow import CommandContext, Empty
from app.core.workspace.commands_base import WorkspaceOpCommand

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.agent_gateway import AgentCommand

log = structlog.get_logger("core.workspace.commands.refresh_auth")


class RefreshWorkspaceAuthInputs(BaseModel):
    """Typed inputs for the RefreshWorkspaceAuth recovery command."""

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID


class RefreshWorkspaceAuthOutputs(Empty):
    """No outputs from RefreshWorkspaceAuth."""


class RefreshWorkspaceAuth(WorkspaceOpCommand):
    """Recovery command that rotates workspace auth credentials.

    Dispatches a placeholder `CleanupWorkspaceCommand` as the wire payload
    (a real `RefreshWorkspaceAuth` AgentCommand type is a future addition).
    `recovers_failure_label = "auth_expired"` is the key the engine uses to
    match this command to failing steps. Registered automatically by
    `WorkflowEngine.register_workflow` via `pr_review_v1.recovery_commands`.
    """

    kind = "RefreshWorkspaceAuth"
    Inputs = RefreshWorkspaceAuthInputs
    Outputs = RefreshWorkspaceAuthOutputs
    needs_claim = False
    recovers_failure_label = "auth_expired"

    async def build_command(
        self,
        inputs: RefreshWorkspaceAuthInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> AgentCommand | None:
        """Return a placeholder AgentCommand for auth refresh."""
        del ctx, session
        log.debug(
            "refresh_workspace_auth.dispatching",
            workspace_id=str(inputs.workspace_id),
        )
        return CleanupWorkspaceCommand(
            command_id=uuid7(),
            workspace_id=inputs.workspace_id,
            traceparent="",
        )
