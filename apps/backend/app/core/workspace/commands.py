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
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select

from app.core.agent_gateway import (
    AuthBlock,
    CleanupWorkspaceCommand,
    RepoRef,
    enqueue_command,
    pin_command_to_agent,
)
from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.remote_provider import dispatch_provision_workspace
from app.core.workspace.service import close_workspace, create_workspace, list_workspace_providers
from app.core.workspace.types import NetworkPolicy, RepoRefForSpec, ResourceCaps, WorkspaceSpec
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
    the wire; `execute()` is not called by the engine. It is callable
    directly in unit tests that exercise the body in isolation — it finds
    the single registered provider, fetches ticket context via the
    `WorkflowContextProvider`, and calls `create_workspace()`.

    Falls back to `Outcome.failure` when:
    - no provider is registered
    - the ticket context is not found
    - `create_workspace()` raises
    """

    kind = "ProvisionWorkspace"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs
        providers = list_workspace_providers()
        if not providers:
            return Outcome.failure(reason="no workspace provider registered")
        provider_id = providers[0].meta.id

        workflow_ctx_provider = get_workflow_context_provider()
        try:
            ticket_ctx = await workflow_ctx_provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        except Exception as exc:
            log.exception(
                "provision_workspace.context_fetch_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        if ticket_ctx is None:
            return Outcome.failure(reason=f"ticket {ctx.ticket_id} not found")

        head_sha = str(ticket_ctx.payload.get("head_sha") or "HEAD")
        base_sha = ticket_ctx.payload.get("base_sha")
        spec = WorkspaceSpec(
            repo=RepoRefForSpec(plugin_id=ticket_ctx.plugin_id, external_id=ticket_ctx.repo_external_id),
            sha=head_sha,
            base_sha=str(base_sha) if base_sha else None,
            resource_caps=ResourceCaps(),
            network_policy=NetworkPolicy.GITHUB_ONLY,
        )
        try:
            ws = await create_workspace(provider_id, spec, org_id=ticket_ctx.org_id)
        except Exception as exc:
            log.exception(
                "provision_workspace.create_failed",
                workflow_execution_id=ctx.workflow_execution_id,
                ticket_id=ctx.ticket_id,
            )
            return Outcome.failure(reason=f"{type(exc).__name__}: {exc}")

        log.info(
            "provision_workspace.success",
            workflow_execution_id=ctx.workflow_execution_id,
            ticket_id=ctx.ticket_id,
            workspace_id=ws.id,
        )
        return Outcome.success(outputs={"workspace_id": ws.id})

    async def dispatch(
        self,
        inputs: dict[str, Any],
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Enqueue a `ProvisionWorkspace` AgentCommand durably inside the
        caller's transaction and return its command_id. Creates the workspace
        row via `create_workspace` so the agent has a target id to provision
        into.

        `RepoRef.clone_url` and `AuthBlock.token` are placeholders: the
        `WorkspaceTicketContext` VO does not yet carry `clone_url`/`installation_token`,
        so the dispatch exercises the correlation path without real auth.
        """
        del inputs
        providers = list_workspace_providers()
        if not providers:
            raise RuntimeError("no workspace provider registered")
        provider_id = providers[0].meta.id

        workflow_ctx_provider = get_workflow_context_provider()
        ticket_ctx = await workflow_ctx_provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
        if ticket_ctx is None:
            raise RuntimeError(f"ticket {ctx.ticket_id} not found")

        head_sha = str(ticket_ctx.payload.get("head_sha") or "HEAD")
        base_sha = ticket_ctx.payload.get("base_sha")
        spec = WorkspaceSpec(
            repo=RepoRefForSpec(plugin_id=ticket_ctx.plugin_id, external_id=ticket_ctx.repo_external_id),
            sha=head_sha,
            base_sha=str(base_sha) if base_sha else None,
            resource_caps=ResourceCaps(),
            network_policy=NetworkPolicy.GITHUB_ONLY,
        )
        ws = await create_workspace(provider_id, spec, org_id=ticket_ctx.org_id)
        ws_id = UUID(ws.id)

        # Placeholder clone_url/token: WorkspaceTicketContext does not yet carry
        # clone_url+installation_token, so these exercise the correlation path
        # end-to-end without depending on real auth.
        repo = RepoRef(
            plugin_id=ticket_ctx.plugin_id,
            external_id=ticket_ctx.repo_external_id,
            clone_url="",
            head_sha=head_sha,
            base_sha=str(base_sha) if base_sha else None,
        )
        auth = AuthBlock(kind="github_installation", token="placeholder")
        result = await dispatch_provision_workspace(
            ticket_ctx.org_id,
            ws_id,
            repo=repo,
            auth=auth,
            traceparent=ctx.traceparent or "",
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
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
        """Enqueue a `CleanupWorkspace` AgentCommand for the prior step's
        workspace_id and return its command_id. Pins to the workspace's
        owning agent via the existing `dispatch_cleanup_workspace` helper
        when the workspace row carries one; otherwise enqueues unpinned for
        any agent to claim (used when the prior provision didn't land a row).

        The unpinned arm covers partial-failure paths where the prior provision
        left a workspace row without an owning agent.
        """
        ws_id_raw = inputs.get("workspace_id")
        if not ws_id_raw:
            raise RuntimeError("CleanupWorkspace.dispatch missing workspace_id input")
        ws_id = UUID(str(ws_id_raw))

        ws_row = (
            await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws_id))
        ).scalar_one_or_none()
        if ws_row is None:
            raise RuntimeError(f"workspace {ws_id} not found for cleanup dispatch")
        org_id = ws_row.org_id
        owning_agent_id = ws_row.owning_agent_id

        command_id = uuid4()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=ws_id,
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        if owning_agent_id is not None:
            await pin_command_to_agent(command_id, owning_agent_id, session=session)
        return command_id


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
        log.info(
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
        """Enqueue a placeholder AgentCommand for auth refresh and return the
        new command_id. The real `RefreshWorkspaceAuth` AgentCommand kind is not
        yet wired over the wire; this enqueues a `CleanupWorkspace`-shaped no-op
        against the recovering workspace so the correlation path is exercised.
        The label on the recovery policy in
        `core/workspace.register_recovery_policy` is unchanged.
        """
        ws_id_raw = inputs.get("workspace_id")
        ws_id: UUID | None = None
        if ws_id_raw:
            ws_id = UUID(str(ws_id_raw))

        org_id: UUID
        owning_agent_id: UUID | None = None
        if ws_id is not None:
            ws_row = (
                await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws_id))
            ).scalar_one_or_none()
            if ws_row is None:
                raise RuntimeError(f"workspace {ws_id} not found for refresh-auth dispatch")
            org_id = ws_row.org_id
            owning_agent_id = ws_row.owning_agent_id
        else:
            raise RuntimeError("RefreshWorkspaceAuth.dispatch missing workspace_id input")

        command_id = uuid4()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=ws_id,
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        if owning_agent_id is not None:
            await pin_command_to_agent(command_id, owning_agent_id, session=session)
        return command_id


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
