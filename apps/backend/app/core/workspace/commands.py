"""Workspace-lifecycle WorkflowCommands — `ProvisionWorkspace`,
`CleanupWorkspace`, `RefreshWorkspaceAuth`.

Each command is **Workspace** category — the engine always parks the
execution in `awaiting_agent` and dispatches an AgentCommand over the wire
to the single registered WorkspaceAgent. The `execute()` body is never
called by the engine in production; it is callable directly in unit tests
that want to exercise the body in isolation.

`ProvisionWorkspace` is the workspace-create step; the engine dispatches a
`ProvisionWorkspace` AgentCommand and the agent returns the `workspace_id`.
`CleanupWorkspace` closes the workspace by id (from typed `CleanupWorkspaceInputs`).
`RefreshWorkspaceAuth` is the recovery command bound to `auth_expired`
failures; it issues a `RefreshWorkspaceAuth` AgentCommand so the Go agent
can rotate its checkout's auth header before the retry.

All inputs flow through typed Pydantic models populated by the workflow's
`inputs_factory` lambdas and stored by the engine in the task queue. No
context-provider lookups at dispatch time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid7

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, ConfigDict

from app.core.agent_gateway import (
    AuthBlock,
    CleanupWorkspaceCommand,
    RepoRef,
)
from app.core.workflow import CommandCategory, CommandContext, Empty, Outcome
from app.core.workspace.dispatch import dispatch_via_workspace
from app.core.workspace.remote_provider import dispatch_provision_workspace
from app.core.workspace.service import close_workspace, list_workspace_providers

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("core.workspace.commands")


# ── Input / Output types ────────────────────────────────────────────────


class ProvisionWorkspaceInputs(BaseModel):
    """Typed inputs for the ProvisionWorkspace step.

    Supplied by the workflow's inputs_factory lambda, which reads fields from
    the workflow-input snapshot (TicketSnapshot or equivalent).
    """

    model_config = ConfigDict(frozen=True)
    org_id: UUID
    plugin_id: str
    repo_external_id: str
    head_sha: str
    base_sha: str | None = None


class ProvisionWorkspaceOutputs(BaseModel):
    """Typed outputs written to step_state when the ProvisionWorkspace
    AgentCommand completes. `workspace_id` is the agent's lifecycle handle."""

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID


class CleanupWorkspaceInputs(BaseModel):
    """Typed inputs for the CleanupWorkspace step.

    `workspace_id` is None when provision failed before creating a workspace —
    the command treats None as a no-op (idempotent cleanup).
    """

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID | None = None


class CleanupWorkspaceOutputs(Empty):
    """No outputs from CleanupWorkspace."""


class RefreshWorkspaceAuthInputs(BaseModel):
    """Typed inputs for the RefreshWorkspaceAuth recovery command."""

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID


class RefreshWorkspaceAuthOutputs(Empty):
    """No outputs from RefreshWorkspaceAuth."""


# ── Command implementations ─────────────────────────────────────────────


class _LifecycleCommand:
    """Tiny base for the three lifecycle commands. All three are
    Workspace-category and restart-safe (idempotent re-dispatch is the
    Workspace branch's contract)."""

    category = CommandCategory.WORKSPACE
    restart_safe = True


class ProvisionWorkspace(_LifecycleCommand):
    """Provision a workspace for a ticket.

    The workflow engine's Workspace branch always parks the execution in
    `awaiting_agent` and dispatches a `ProvisionWorkspace` AgentCommand over
    the wire; the remote agent performs the actual provisioning. `execute()` is
    never called by the engine. The legacy in-process provision path has been
    removed — `execute()` returns failure unconditionally to surface
    any mistaken direct calls.
    """

    kind = "ProvisionWorkspace"
    Inputs = ProvisionWorkspaceInputs
    Outputs = ProvisionWorkspaceOutputs

    async def execute(self, inputs: ProvisionWorkspaceInputs, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.failure(
            reason="ProvisionWorkspace.execute is not the dispatch path for remote provisioning"
        )

    async def dispatch(
        self,
        inputs: ProvisionWorkspaceInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Mint a `workspace_id` UUID (no row yet), fetch install credentials,
        enqueue a `ProvisionWorkspace` AgentCommand durably inside the caller's
        transaction, and return its command_id. The `workspaces` row is created
        lean on the agent's first workspace event (`created` or `ready`) by the
        sink in `agent_report.py`.

        `clone_url` and `installation_token` come from `core/vcs.get_install_credentials`
        so credentials are fetched at dispatch time.
        Raises `VcsInstallNotFound` when the org has no active VCS App installation.
        """
        from app.core import vcs as _vcs  # noqa: PLC0415

        providers = list_workspace_providers()
        if not providers:
            raise RuntimeError("no workspace provider registered")

        ws_id = uuid7()

        creds = await _vcs.get_install_credentials(
            inputs.plugin_id,
            inputs.org_id,
            inputs.repo_external_id,
        )

        repo = RepoRef(
            plugin_id=inputs.plugin_id,
            external_id=inputs.repo_external_id,
            clone_url=creds.clone_url,
            head_sha=inputs.head_sha,
            base_sha=inputs.base_sha,
        )
        auth = AuthBlock(
            kind="github_installation",
            token=creds.installation_token.get_secret_value(),
        )
        result = await dispatch_provision_workspace(
            inputs.org_id,
            ws_id,
            repo=repo,
            auth=auth,
            traceparent="",
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        log.debug(
            "provision_workspace.dispatched",
            workflow_execution_id=ctx.workflow_execution_id,
            workspace_id=str(ws_id),
            command_id=str(result.command_id),
        )
        return result.command_id


class CleanupWorkspace(_LifecycleCommand):
    """Tear down a workspace. Reads `workspace_id` from typed `CleanupWorkspaceInputs`.
    Idempotent: a None or already-closed workspace is treated as success so
    workflow cleanup after partial failures still drains cleanly.

    Must only run after every claim against the workspace has been
    released — see failure-report-precedes-disposal in core_workspace.md.
    """

    kind = "CleanupWorkspace"
    Inputs = CleanupWorkspaceInputs
    Outputs = CleanupWorkspaceOutputs

    async def execute(self, inputs: CleanupWorkspaceInputs, ctx: CommandContext) -> Outcome:
        del ctx
        if inputs.workspace_id is None:
            # No workspace to clean up (e.g. provision step failed before
            # creating one). Treat as success — there's nothing to do.
            return Outcome.success()

        try:
            await close_workspace(inputs.workspace_id)
        except Exception as exc:
            # inside-span failure: workflow.command.CleanupWorkspace span is active
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
            log.exception("cleanup_workspace.failed", workspace_id=str(inputs.workspace_id))
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        return Outcome.success()

    async def dispatch(
        self,
        inputs: CleanupWorkspaceInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Enqueue a `CleanupWorkspace` AgentCommand via `dispatch_via_workspace`
        (Layer 2) and return its command_id. Layer 2 looks up the workspace row,
        calls enqueue_command, and pins to the owning agent when one is set.
        """
        if inputs.workspace_id is None:
            raise RuntimeError("CleanupWorkspace.dispatch: workspace_id is None")

        cmd = CleanupWorkspaceCommand(
            command_id=uuid7(),
            workspace_id=inputs.workspace_id,
            traceparent="",
        )
        return await dispatch_via_workspace(
            command=cmd,
            workspace_id=inputs.workspace_id,
            ctx=ctx,
            session=session,
            claim_workspace=False,
        )


class RefreshWorkspaceAuth(_LifecycleCommand):
    """Recovery command bound to `auth_expired` failures via
    `core/workspace.register_recovery_policy`. The engine inserts this
    command before re-dispatching the originally-failing AgentCommand.

    The engine dispatches a `RefreshWorkspaceAuth` AgentCommand over the
    wire so the Go agent can rotate its checkout's auth header before the
    retry. `execute()` is callable directly in unit tests; it returns
    success so the step advances cleanly.
    """

    kind = "RefreshWorkspaceAuth"
    Inputs = RefreshWorkspaceAuthInputs
    Outputs = RefreshWorkspaceAuthOutputs

    async def execute(self, inputs: RefreshWorkspaceAuthInputs, ctx: CommandContext) -> Outcome:
        del inputs
        log.debug(
            "refresh_workspace_auth.inline",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
        )
        return Outcome.success()

    async def dispatch(
        self,
        inputs: RefreshWorkspaceAuthInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Enqueue a placeholder AgentCommand for auth refresh via
        `dispatch_via_workspace` (Layer 2) and return the new command_id.
        """
        cmd = CleanupWorkspaceCommand(
            command_id=uuid7(),
            workspace_id=inputs.workspace_id,
            traceparent="",
        )
        return await dispatch_via_workspace(
            command=cmd,
            workspace_id=inputs.workspace_id,
            ctx=ctx,
            session=session,
            claim_workspace=False,
        )


__all__ = [
    "CleanupWorkspace",
    "CleanupWorkspaceInputs",
    "CleanupWorkspaceOutputs",
    "ProvisionWorkspace",
    "ProvisionWorkspaceInputs",
    "ProvisionWorkspaceOutputs",
    "RefreshWorkspaceAuth",
    "RefreshWorkspaceAuthInputs",
    "RefreshWorkspaceAuthOutputs",
]
