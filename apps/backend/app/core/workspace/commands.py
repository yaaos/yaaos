"""Workspace-lifecycle WorkflowCommands — `ProvisionWorkspace`,
`CleanupWorkspace`, `RefreshWorkspaceAuth`.

Phase 4 (foundations) ships placeholder bodies so the workflow registry
is complete and the five reviewer workflows can register their step
references. Full bodies wired to `core/workspace.create_workspace` /
`close_workspace` / VCS-auth refresh land in a follow-on iteration.

Categorized as **Workspace** since each one issues at least one
AgentCommand under the hood in the eventual implementation. Until then
the engine's `start_step` Workspace branch sets `state=awaiting_agent` +
synthesizes a `pending_agent_command_id` (per Phase 1 cont'd stub) —
these classes are wired the same way as real Workspace commands.
"""

from __future__ import annotations

from typing import Any

from app.core.workflow import CommandCategory, CommandContext, Outcome


class _LifecycleCommand:
    """Tiny base for the three lifecycle commands. All three are
    Workspace-category and restart-safe (idempotent re-dispatch is the
    Workspace branch's contract)."""

    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: dict[str, Any], ctx: CommandContext) -> Outcome:
        del inputs, ctx
        # Stub. Real bodies land in a follow-on Phase 4 iteration.
        return Outcome.success()


class ProvisionWorkspace(_LifecycleCommand):
    """Provision a workspace for a ticket's repo + head sha. Will issue
    `CreateWorkspace` and (when org has yaaos skills configured)
    `WriteFiles` AgentCommands in the full implementation."""

    kind = "ProvisionWorkspace"


class CleanupWorkspace(_LifecycleCommand):
    """Tear down a workspace. Issues `CleanupWorkspace` AgentCommand. Must
    only run after every claim against the workspace has been released —
    see failure-report-precedes-disposal in core_workspace.md."""

    kind = "CleanupWorkspace"


class RefreshWorkspaceAuth(_LifecycleCommand):
    """Recovery command bound to `auth_expired` failures via
    `core/workspace.register_recovery_policy`. Refreshes the workspace's
    VCS auth credentials and re-dispatches the original command."""

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
