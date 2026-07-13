"""plugins/claude_code — Claude Code CLI wrapper for core/coding_agent."""

from app.plugins.claude_code.service import ClaudeCodePlugin, bootstrap, set_claude_code_plugin_for_tests

__all__ = [
    "ClaudeCodePlugin",
    "bootstrap",
    "set_claude_code_plugin_for_tests",
]

# Register at import time.
bootstrap()
