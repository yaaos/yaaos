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

# Side-effect import: register HTTP routes (/defaults, /repos/...).
from app.plugins.claude_code import web  # noqa: E402, F401
