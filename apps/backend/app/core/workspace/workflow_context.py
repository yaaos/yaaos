"""Workflow-context callback registry — dependency inversion for
WorkspaceCommand bodies that need ticket fields.

`core/workspace` can't import `domain/tickets` (layer rule: core < domain).
But Workspace-category WorkflowCommands like `ProvisionWorkspace` need
the ticket's `org_id`, `plugin_id`, `repo_external_id`, and `payload` to
build a `WorkspaceSpec`. The fix is dependency inversion: domain/reviewer
(or whichever domain owns the workflow) registers a reader callback at
boot; `core/workspace.commands` calls it when needed.

The Protocol is the contract; concrete implementations live in domain
modules. Only one provider may be registered at a time; tests reset via
the `workflow_context_provider_isolation` fixture in `app/testing/isolation`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, SecretStr


class WorkspaceTicketContext(BaseModel):
    """Everything a Workspace-category or Local WorkflowCommand needs from
    a ticket. Returned by the registered provider. Pydantic-frozen so
    callers can't accidentally mutate before passing into dispatch helpers.

    `pr_id` is None when the ticket isn't (yet) associated with a PR row —
    intake creates the ticket before PR materialization in some flows, so
    Local commands that need the reviewer aggregate must handle the None
    case (typically by returning success-without-action).

    `clone_url` and `installation_token` are populated by the registered
    `WorkflowContextProvider` implementation (in `domain/reviewer`) so that
    `ProvisionWorkspace.dispatch` can build the `RepoRef` + `AuthBlock`
    without importing `vcs` or `plugins/github` (layer rule: core < domain).
    Both default to empty/None so provider impls that only populate org-level
    context can omit them; dispatch validates presence before use.
    """

    model_config = {"frozen": True}

    org_id: UUID
    plugin_id: str
    repo_external_id: str
    payload: dict[str, Any]
    pr_id: UUID | None = None
    # Git clone URL for the repo (e.g. https://github.com/org/repo.git).
    # Populated by the domain/reviewer WorkflowContextProvider impl.
    clone_url: str = ""
    # GitHub installation token for the clone. Populated by the
    # domain/reviewer WorkflowContextProvider impl at dispatch time
    # (~1h TTL; agent claims within seconds-minutes).
    installation_token: SecretStr = SecretStr("")


@runtime_checkable
class WorkflowContextProvider(Protocol):
    """Implemented by a domain module (typically `domain/reviewer` or
    `domain/tickets`) and registered at boot. Read-only — no side effects."""

    async def get_workspace_ticket_context(self, ticket_id: UUID) -> WorkspaceTicketContext | None: ...


_PROVIDER: WorkflowContextProvider | None = None


def register_workflow_context_provider(provider: WorkflowContextProvider) -> None:
    """Install the singleton workflow-context reader. Replaces any prior
    registration silently — the bootstrap path may re-register on module
    reload and there's only ever one logical provider in the process."""
    global _PROVIDER
    _PROVIDER = provider


def get_workflow_context_provider() -> WorkflowContextProvider:
    """Read the registered provider. Raises RuntimeError when no provider
    has been installed — a missing provider is a boot-time wiring bug.
    `assert_workflow_context_provider()` is the startup-check entry point."""
    if _PROVIDER is None:
        raise RuntimeError("workflow_context provider not registered")
    return _PROVIDER


def assert_workflow_context_provider() -> None:
    """Assert the provider is installed. Called from web.py / worker.py
    after domain/reviewer import so a wiring bug crashes the process at
    startup rather than surfacing mid-flow."""
    get_workflow_context_provider()  # raises if unbound


def _clear_workflow_context_provider_for_tests() -> None:
    """Clear the registered workflow-context provider. For test isolation
    only — call via the `workflow_context_provider_isolation` fixture in
    `app/testing/isolation`, never from production code."""
    global _PROVIDER
    _PROVIDER = None


__all__ = [
    "WorkflowContextProvider",
    "WorkspaceTicketContext",
    "assert_workflow_context_provider",
    "get_workflow_context_provider",
    "register_workflow_context_provider",
]
