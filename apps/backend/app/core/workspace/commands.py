"""Workspace-lifecycle WorkflowCommands — `ProvisionWorkspace`,
`CleanupWorkspace`, `RefreshWorkspaceAuth`.

Each command is **Workspace** category — the engine always parks the
execution in `awaiting_agent` and dispatches an AgentCommand over the wire
to the single registered WorkspaceAgent. The `execute()` body is never
called by the engine in production; it is callable directly in unit tests
that want to exercise the body in isolation.

`ProvisionWorkspace` is the workspace-create step; the engine dispatches a
`ProvisionWorkspace` AgentCommand and the agent returns the `workspace_id`.
`CleanupWorkspace` closes the workspace by id (from prior step outputs).
`RefreshWorkspaceAuth` is the recovery command bound to `auth_expired`
failures; it issues a `RefreshWorkspaceAuth` AgentCommand so the Go agent
can rotate its checkout's auth header before the retry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid7

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.core.agent_gateway import (
    AuthBlock,
    CleanupWorkspaceCommand,
    RepoRef,
)
from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace.dispatch import dispatch_via_workspace
from app.core.workspace.remote_provider import dispatch_provision_workspace
from app.core.workspace.service import close_workspace, list_workspace_providers
from app.core.workspace.workflow_context import get_workflow_context_provider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("core.workspace.commands")


class _LifecycleCommand:
    """Tiny base for the three lifecycle commands. All three are
    Workspace-category and restart-safe (idempotent re-dispatch is the
    Workspace branch's contract)."""

    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


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

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.failure(
            reason="ProvisionWorkspace.execute is not the dispatch path for remote provisioning"
        )

    async def dispatch(
        self,
        inputs: dict[str, Any],
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
        rather than the workflow-context provider, so credentials are fetched
        at dispatch time and the provider no longer needs to carry them.
        Raises `VcsInstallNotFound` when the org has no active VCS App installation.
        """
        from app.core import vcs as _vcs  # noqa: PLC0415

        del inputs
        providers = list_workspace_providers()
        if not providers:
            raise RuntimeError("no workspace provider registered")

        workflow_ctx_provider = get_workflow_context_provider()
        ticket_ctx = await workflow_ctx_provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        if ticket_ctx is None:
            raise RuntimeError(f"ticket {ctx.ticket_id} not found")

        head_sha = str(ticket_ctx.payload.get("head_sha") or "HEAD")
        base_sha = ticket_ctx.payload.get("base_sha")

        # Mint the workspace_id up front — the row is created lean on the agent's
        # first workspace event, not here. The UUID is the agent's lifecycle handle
        # and the WorkspaceEvent key.
        ws_id = uuid7()

        # Fetch credentials at dispatch time — fresh token, no stale data in ctx.
        creds = await _vcs.get_install_credentials(
            ticket_ctx.plugin_id,
            ticket_ctx.org_id,
            ticket_ctx.repo_external_id,
        )

        repo = RepoRef(
            plugin_id=ticket_ctx.plugin_id,
            external_id=ticket_ctx.repo_external_id,
            clone_url=creds.clone_url,
            head_sha=head_sha,
            base_sha=str(base_sha) if base_sha else None,
        )
        auth = AuthBlock(
            kind="github_installation",
            token=creds.installation_token.get_secret_value(),
        )
        result = await dispatch_provision_workspace(
            ticket_ctx.org_id,
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
    """Tear down a workspace. Reads `workspace_id` from inputs (typically
    sourced from the prior `ProvisionWorkspace` step via the workflow
    `$provision.workspace_id` input expression). Idempotent: a missing or
    already-closed workspace is treated as success so workflow cleanup
    after partial failures still drains cleanly.

    Must only run after every claim against the workspace has been
    released — see failure-report-precedes-disposal in core_workspace.md.
    """

    kind = "CleanupWorkspace"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del ctx
        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            # No workspace to clean up (e.g. provision step failed before
            # creating one). Treat as success — there's nothing to do.
            return Outcome.success()
        try:
            ws_id = UUID(str(ws_id_raw))
        except TypeError, ValueError:
            return Outcome.failure(reason=f"invalid workspace_id: {ws_id_raw!r}")

        try:
            await close_workspace(ws_id)
        except Exception as exc:
            # inside-span failure: workflow.command.CleanupWorkspace span is active
            span = trace.get_current_span()
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}")
            log.exception("cleanup_workspace.failed", workspace_id=str(ws_id))
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        return Outcome.success()

    async def dispatch(
        self,
        inputs: dict[str, Any],
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Enqueue a `CleanupWorkspace` AgentCommand via `dispatch_via_workspace`
        (Layer 2) and return its command_id. Layer 2 looks up the workspace row,
        calls enqueue_command, and pins to the owning agent when one is set.
        """
        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            raise RuntimeError("CleanupWorkspace.dispatch missing workspace_id input")
        ws_id = UUID(str(ws_id_raw))

        cmd = CleanupWorkspaceCommand(
            command_id=uuid7(),
            workspace_id=ws_id,
            traceparent="",
        )
        return await dispatch_via_workspace(
            command=cmd,
            workspace_id=ws_id,
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

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs
        log.debug(
            "refresh_workspace_auth.inline",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
        )
        return Outcome.success()

    async def dispatch(
        self,
        inputs: dict[str, Any],
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Enqueue a placeholder AgentCommand for auth refresh via
        `dispatch_via_workspace` (Layer 2) and return the new command_id.
        The real `RefreshWorkspaceAuth` AgentCommand kind is not yet wired
        over the wire; this enqueues a `CleanupWorkspace`-shaped no-op against
        the recovering workspace so the correlation path is exercised.
        """
        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            raise RuntimeError("RefreshWorkspaceAuth.dispatch missing workspace_id input")
        ws_id = UUID(str(ws_id_raw))

        cmd = CleanupWorkspaceCommand(
            command_id=uuid7(),
            workspace_id=ws_id,
            traceparent="",
        )
        return await dispatch_via_workspace(
            command=cmd,
            workspace_id=ws_id,
            ctx=ctx,
            session=session,
            claim_workspace=False,
        )


ALL_LIFECYCLE_COMMANDS: tuple[_LifecycleCommand, ...] = (
    ProvisionWorkspace(),
    CleanupWorkspace(),
    RefreshWorkspaceAuth(),
)


__all__ = [
    "ALL_LIFECYCLE_COMMANDS",
    "CleanupWorkspace",
    "ProvisionWorkspace",
    "RefreshWorkspaceAuth",
]
