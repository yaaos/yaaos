"""Workspace-lifecycle WorkflowCommands — `ProvisionWorkspace`,
`CleanupWorkspace`, `RefreshWorkspaceAuth`.

Categorized as **Workspace** since each one issues at least one
AgentCommand under the hood for the remote_agent provider. For the
in_memory provider, the engine's `start_step` Workspace branch runs
them inline (see [core_workflow.md § Workspace dispatch](core_workflow.md))
and these bodies own the workspace lifecycle calls directly.

`CleanupWorkspace` has a real in-memory body — it only needs the
`workspace_id` from inputs, no cross-layer reads required.
`ProvisionWorkspace` is a stub: a real body needs to read ticket
fields (org_id, repo, sha) but `core/workspace` can't import
`domain/tickets` (layer rule). It reads ticket context through a
dependency-inversion callback registered by domain/reviewer at boot.
`RefreshWorkspaceAuth` is a stub pending the auth-refresh substrate.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace.service import close_workspace, create_workspace
from app.core.workspace.types import (
    NetworkPolicy,
    RepoRefForSpec,
    ResourceCaps,
    WorkspaceSpec,
)
from app.core.workspace.workflow_context import get_workflow_context_provider

log = structlog.get_logger("core.workspace.commands")

# Provider id used for the in-memory workspace plugin.
_IN_MEMORY_PROVIDER_ID = "in_process"


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
    """Provision a workspace for a ticket's repo + head sha. For the
    in_memory provider, fetches the ticket context via the registered
    `WorkflowContextProvider`, builds a `WorkspaceSpec`, and calls
    `create_workspace()` directly. Returns the new `workspace_id` in
    outputs so downstream steps can claim against it via the `$provision`
    input expression. For the remote_agent provider this issues
    `CreateWorkspace` over the wire to the Go workspace subcommand.

    Falls back to `Outcome.failure` when:
    - no `WorkflowContextProvider` is registered (bootstrap bug)
    - the provider returns None (ticket not found)
    - `create_workspace()` raises (provider-level provisioning failure)
    """

    kind = "ProvisionWorkspace"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs
        provider = get_workflow_context_provider()
        if provider is None:
            return Outcome.failure(reason="no workflow_context provider registered")

        try:
            ticket_ctx = await provider.get_workspace_ticket_context(UUID(ctx.ticket_id))
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
            ws = await create_workspace(_IN_MEMORY_PROVIDER_ID, spec, org_id=ticket_ctx.org_id)
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
        except (TypeError, ValueError):
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

    For the **in_memory** provider this is a no-op: the in-process
    provider fetches a fresh installation token on each `git fetch`/clone
    (see `plugins/in_memory_workspace/service.py`), so there's no stored
    credential to refresh. Returning success lets the engine append the
    re-dispatch step cleanly.

    For the **remote_agent** provider this issues a `RefreshWorkspaceAuth`
    AgentCommand over the wire so the Go agent can rotate its checkout's
    auth header before the retry.
    """

    kind = "RefreshWorkspaceAuth"

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs
        log.info(
            "refresh_workspace_auth.no_op_in_memory",
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
