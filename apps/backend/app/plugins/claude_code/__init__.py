"""plugins/claude_code — Claude Code CLI wrapper for domain/coding_agent."""

from app.plugins.claude_code.models import ClaudeCodeSettingsRow
from app.plugins.claude_code.service import ClaudeCodePlugin, bootstrap, get_plugin, set_api_key

__all__ = ["ClaudeCodePlugin", "ClaudeCodeSettingsRow", "bootstrap", "get_plugin", "set_api_key"]

# Register at import time.
bootstrap()

# Side-effect import: register HTTP routes (/api/claude_code/api_key, /health).
from app.plugins.claude_code import web  # noqa: E402, F401
