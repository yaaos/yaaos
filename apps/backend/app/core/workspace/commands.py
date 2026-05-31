"""Workspace-lifecycle WorkflowCommands — `ProvisionWorkspace`,
`CleanupWorkspace`, `RefreshWorkspaceAuth`.

Each command is **Workspace** category — the engine parks the execution in
`awaiting_agent` and dispatches an AgentCommand over the wire to the
remote WorkspaceAgent.

`CleanupWorkspace` closes the workspace by id.
`ProvisionWorkspace` is a workspace-category command; the engine parks the
step in `awaiting_agent` and dispatches a `CreateWorkspace` AgentCommand —
the `execute()` body is only reached on the stub in-process dispatch path
used in tests.
`RefreshWorkspaceAuth` is the recovery command bound to `auth_expired`
failures; it issues a `RefreshWorkspaceAuth` AgentCommand so the Go agent
can rotate its checkout's auth header before the retry.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace.service import close_workspace, create_workspace, list_workspace_providers
from app.core.workspace.types import NetworkPolicy, RepoRefForSpec, ResourceCaps, WorkspaceSpec
from app.core.workspace.workflow_context import get_workflow_context_provider

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

    The workflow engine's Workspace branch parks the execution in
    `awaiting_agent` and dispatches a `CreateWorkspace` AgentCommand over
    the wire for the `remote_agent` path; `execute()` is never called on
    that path. On the in-process dispatch path (tests that register a stub
    provider), `execute()` is called inline — it finds the first registered
    provider, fetches ticket context via the `WorkflowContextProvider`, and
    calls `create_workspace()` with it.

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


class RefreshWorkspaceAuth(_LifecycleCommand):
    """Recovery command bound to `auth_expired` failures via
    `core/workspace.register_recovery_policy`. The engine inserts this
    command before re-dispatching the originally-failing AgentCommand.

    The workflow engine's Workspace branch dispatches a `RefreshWorkspaceAuth`
    AgentCommand over the wire so the Go agent can rotate its checkout's
    auth header before the retry. This `execute()` body is only reached on
    the in-process dispatch path (stub tests); it returns success so the
    step advances cleanly.
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
