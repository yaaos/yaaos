"""Concrete `WorkflowContextProvider` for `plugins/claude_code`.

Wraps `domain/tickets.get_workspace_ticket_context` and populates
`clone_url` + `installation_token` from the VCS plugin. This provider
works for any ticket carrying a repo — both `skill_enumeration` and
`pr_review` types. Registered at module import so the single process-wide
provider always has VCS credentials.

`plugins/claude_code → core/vcs` is a legal downward import
(plugins > core). `core/workspace` never imports `core/vcs` — only
this concrete implementation does, keeping the layer rule intact.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import SecretStr

from app.core.vcs import get_installation_token
from app.core.workspace import WorkspaceTicketContext, register_workflow_context_provider
from app.domain.tickets import get_workspace_ticket_context as _get_ticket_ctx


class _ClaudeCodeWorkflowContextProvider:
    """Generic context provider: reads any ticket row, enriches with VCS
    clone credentials. Registered by `bootstrap_workflow_context()` at
    `plugins/claude_code` import time so enumerate and review workflows
    both get real auth."""

    async def get_workspace_ticket_context(self, ticket_id: UUID) -> WorkspaceTicketContext | None:
        ctx = await _get_ticket_ctx(ticket_id)
        if ctx is None:
            return None

        # `clone_url` from payload (set by the refresh endpoint) or derived
        # from `repo_full_name` in the ticket payload.
        repo_full_name: str = (
            ctx.payload.get("repo_full_name")
            or ctx.payload.get("head_repo_full")
            or ctx.repo_external_id  # fallback: external_id is full_name for GitHub
        )

        # Clone URL: https://github.com/<owner>/<repo>.git
        # The GitHub shape is inlined here because `plugins/claude_code →
        # plugins/github` is a forbidden cross-plugin edge; the canonical
        # derivation belongs in `core/vcs`. The fallback returns "" when
        # repo_full_name is empty.
        #
        # Built from `github_git_base_url` (the host the *agent* clones from),
        # not `github_web_base_url` (the browser-facing host) — these diverge in
        # the test stack where the agent container can't reach the host-mapped
        # web URL. Falls back to the web base when the git base is unset (prod).
        from app.core.config import get_settings  # noqa: PLC0415

        settings = get_settings()
        git_base = settings.github_git_base_url or settings.github_web_base_url
        clone_url = f"{git_base}/{repo_full_name}.git" if repo_full_name else ""

        # Installation token — fresh per-dispatch (~1h TTL).
        raw_token = ""
        if ctx.plugin_id and ctx.org_id:
            try:
                raw_token = await get_installation_token(ctx.plugin_id, ctx.org_id)
            except Exception:
                # Non-fatal: provision dispatch validates and the agent will
                # report auth failure, which feeds the recovery policy.
                pass

        return WorkspaceTicketContext(
            org_id=ctx.org_id,
            plugin_id=ctx.plugin_id,
            repo_external_id=ctx.repo_external_id,
            payload=ctx.payload,
            pr_id=ctx.pr_id,
            clone_url=clone_url,
            installation_token=SecretStr(raw_token),
        )


def bootstrap_workflow_context() -> None:
    """Register the concrete provider. Called once at `plugins/claude_code`
    import time. Replaces any prior registration (the reviewer's simpler
    provider doesn't populate clone credentials)."""
    register_workflow_context_provider(_ClaudeCodeWorkflowContextProvider())
