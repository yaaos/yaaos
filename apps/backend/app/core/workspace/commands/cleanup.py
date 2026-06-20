"""CleanupWorkspace — workspace teardown AgentDispatchCommand.

Inherits from `WorkspaceOpCommand`. When `workspace_id` is None (provision
failed before creating a workspace), `build_command` returns None and the
`@final dispatch` raises `_NullDispatch`, which the engine catches and
treats as `Outcome.success()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid7

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict

from app.core.agent_gateway import CleanupWorkspaceCommand
from app.core.workflow import CommandContext, Empty, Outcome
from app.core.workspace.commands_base import WorkspaceOpCommand
from app.core.workspace.service import close_workspace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.agent_gateway import AgentCommand

log = structlog.get_logger("core.workspace.commands.cleanup")


class CleanupWorkspaceInputs(BaseModel):
    """Typed inputs for the CleanupWorkspace step.

    `workspace_id` is None when provision failed before creating a workspace —
    `build_command` returns None in that case, triggering `_NullDispatch`.
    """

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID | None = None


class CleanupWorkspaceOutputs(Empty):
    """No outputs from CleanupWorkspace."""


class CleanupWorkspace(WorkspaceOpCommand):
    """Tear down a workspace. Reads `workspace_id` from typed `CleanupWorkspaceInputs`.

    When `workspace_id` is None, `build_command` returns None which triggers
    `_NullDispatch` in the `@final dispatch`. The engine catches `_NullDispatch`
    and treats the step as `Outcome.success()` — idempotent cleanup after
    partial failures drains cleanly.

    Must only run after every claim against the workspace has been
    released — see failure-report-precedes-disposal in core_workspace.md.
    """

    kind = "CleanupWorkspace"
    Inputs = CleanupWorkspaceInputs
    Outputs = CleanupWorkspaceOutputs
    needs_claim = False

    async def execute(self, inputs: CleanupWorkspaceInputs, ctx: CommandContext) -> Outcome:
        """Inline execute path — not called by the engine; available for tests."""
        del ctx
        if inputs.workspace_id is None:
            return Outcome.success()

        try:
            await close_workspace(inputs.workspace_id)
        except Exception as exc:
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
            log.exception("cleanup_workspace.failed", workspace_id=str(inputs.workspace_id))
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        return Outcome.success()

    async def build_command(
        self,
        inputs: CleanupWorkspaceInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> AgentCommand | None:
        """Return a CleanupWorkspaceCommand, or None when workspace_id is None."""
        if inputs.workspace_id is None:
            return None

        return CleanupWorkspaceCommand(
            command_id=uuid7(),
            workspace_id=inputs.workspace_id,
            traceparent="",
        )
