"""Workspace-lifecycle WorkflowCommands — `ProvisionWorkspace`,
`CleanupWorkspace`, `RefreshWorkspaceAuth`.

Categorized as **Workspace** since each one issues at least one
AgentCommand under the hood for the remote_agent provider. For the
in_memory provider, the engine's `start_step` Workspace branch runs
them inline (see [core_workflow.md § Workspace dispatch](core_workflow.md))
and these bodies own the workspace lifecycle calls directly.

`CleanupWorkspace` ships with a real in-memory body — it only needs the
`workspace_id` from inputs, no cross-layer reads required.
`ProvisionWorkspace` remains a stub: a real body needs to read ticket
fields (org_id, repo, sha) but `core/workspace` can't import
`domain/tickets` (layer rule). The fix is a dependency-inversion
callback registered by domain/reviewer at boot, landing in the next
follow-on slice. `RefreshWorkspaceAuth` remains a stub pending the
auth-refresh substrate.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.core.workflow import CommandCategory, CommandContext, Outcome
from app.core.workspace.service import close_workspace

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
    """Provision a workspace for a ticket's repo + head sha. Will issue
    `CreateWorkspace` and (when org has yaaos skills configured)
    `WriteFiles` AgentCommands in the full implementation. Body lands in
    the follow-on slice that introduces the ticket-reader callback."""

    kind = "ProvisionWorkspace"


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
    `core/workspace.register_recovery_policy`. Refreshes the workspace's
    VCS auth credentials and re-dispatches the original command. Body
    lands alongside the VCS-auth substrate."""

    kind = "RefreshWorkspaceAuth"


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
