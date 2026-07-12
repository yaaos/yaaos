"""plugins/codex — OpenAI Codex CLI wrapper for core/coding_agent."""

from app.plugins.codex.service import CodexPlugin, bootstrap, set_codex_plugin_for_tests

__all__ = [
    "CodexPlugin",
    "bootstrap",
    "set_codex_plugin_for_tests",
]

# Register at import time.
bootstrap()
