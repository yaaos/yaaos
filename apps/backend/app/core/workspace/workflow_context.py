"""Workflow-context callback registry — dependency inversion for
WorkspaceCommand bodies that need ticket fields.

`core/workspace` can't import `domain/tickets` (layer rule: core < domain).
But Workspace-category WorkflowCommands like `ProvisionWorkspace` need
the ticket's `org_id`, `plugin_id`, `repo_external_id`, and `payload` to
build a `WorkspaceSpec`. The fix is dependency inversion: domain/reviewer
(or whichever domain owns the workflow) registers a reader callback at
boot; `core/workspace.commands` calls it when needed.

The Protocol is the contract; concrete implementations live in domain
modules. Only one provider may be registered at a time; tests can reset
via `clear_workflow_context_provider()`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel


class WorkspaceTicketContext(BaseModel):
    """Everything a Workspace-category or Local WorkflowCommand needs from
    a ticket. Returned by the registered provider. Pydantic-frozen so
    callers can't accidentally mutate before passing into `create_workspace`.

    `pr_id` is None when the ticket isn't (yet) associated with a PR row —
    intake creates the ticket before PR materialization in some flows, so
    Local commands that need the reviewer aggregate must handle the None
    case (typically by returning success-without-action).
    """

    model_config = {"frozen": True}

    org_id: UUID
    plugin_id: str
    repo_external_id: str
    payload: dict[str, Any]
    pr_id: UUID | None = None


@runtime_checkable
class WorkflowContextProvider(Protocol):
    """Implemented by a domain module (typically `domain/reviewer` or
    `domain/tickets`) and registered at boot. Read-only — no side effects."""

    async def get_workspace_ticket_context(self, ticket_id: UUID) -> WorkspaceTicketContext | None: ...


_PROVIDER: WorkflowContextProvider | None = None


def register_workflow_context_provider(provider: WorkflowContextProvider) -> None:
    """Install the singleton workflow-context reader. Replaces any prior
    registration silently — the bootstrap path may re-register on module
    reload (test isolation) and there's only ever one logical provider
    in the process."""
    global _PROVIDER
    _PROVIDER = provider


def get_workflow_context_provider() -> WorkflowContextProvider | None:
    """Read the registered provider. Callers must handle None — the bare
    `core/workspace` module ships without a provider; only the full app
    bootstrap installs one."""
    return _PROVIDER


def clear_workflow_context_provider() -> None:
    """Clear the registered workflow-context provider."""
    global _PROVIDER
    _PROVIDER = None


__all__ = [
    "WorkflowContextProvider",
    "WorkspaceTicketContext",
    "clear_workflow_context_provider",
    "get_workflow_context_provider",
    "register_workflow_context_provider",
]
