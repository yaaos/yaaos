"""plugins/claude_code — Claude Code CLI wrapper for core/coding_agent."""

from app.plugins.claude_code.repos import set_repo_skill
from app.plugins.claude_code.service import ClaudeCodePlugin, bootstrap

__all__ = [
    "ClaudeCodePlugin",
    "bootstrap",
    "set_repo_skill",
]

# Register at import time.
bootstrap()

# Register the WorkflowContextProvider that enriches WorkspaceTicketContext
# with clone_url + installation_token. Overwrites the simpler provider
# registered by domain/reviewer (which doesn't fetch VCS credentials).
from app.plugins.claude_code.workflow_context import bootstrap_workflow_context  # noqa: E402

bootstrap_workflow_context()

# Side-effect import: register HTTP routes (/defaults, /repos/...).
from app.plugins.claude_code import web  # noqa: E402, F401
