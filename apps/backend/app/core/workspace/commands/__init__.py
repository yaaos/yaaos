"""Workspace-lifecycle WorkflowCommands.

Three AgentDispatchCommands covering the workspace lifecycle:
- `ProvisionWorkspace` — create a workspace (no existing row; uses Layer 1 directly).
- `CleanupWorkspace` — tear down a workspace (via Layer 2; null-safe via `_NullDispatch`).
- `RefreshWorkspaceAuth` — rotate checkout auth credentials (recovery command).

See `apps/backend/docs/core_workspace.md`.
"""

from app.core.workspace.commands.cleanup import (
    CleanupWorkspace,
    CleanupWorkspaceInputs,
    CleanupWorkspaceOutputs,
)
from app.core.workspace.commands.provision import (
    ProvisionWorkspace,
    ProvisionWorkspaceInputs,
    ProvisionWorkspaceOutputs,
)
from app.core.workspace.commands.refresh_auth import (
    RefreshWorkspaceAuth,
    RefreshWorkspaceAuthInputs,
    RefreshWorkspaceAuthOutputs,
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
