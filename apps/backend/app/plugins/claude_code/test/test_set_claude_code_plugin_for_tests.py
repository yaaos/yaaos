"""Tests for set_claude_code_plugin_for_tests isolation seam."""

import app.plugins.claude_code.service as _svc
from app.plugins.claude_code.service import ClaudeCodePlugin, set_claude_code_plugin_for_tests


def test_set_claude_code_plugin_for_tests_swaps_for_block():
    """Inside the block the singleton is replaced with the provided instance."""
    replacement = ClaudeCodePlugin()
    with set_claude_code_plugin_for_tests(replacement) as got:
        assert got is replacement
        assert _svc._plugin is replacement


def test_set_claude_code_plugin_for_tests_restores_after_exit():
    """The original singleton is restored after block exit."""
    original = _svc._plugin
    replacement = ClaudeCodePlugin()

    with set_claude_code_plugin_for_tests(replacement):
        assert _svc._plugin is replacement

    assert _svc._plugin is original


def test_set_claude_code_plugin_for_tests_restores_on_exception():
    """The original singleton is restored even when the block raises."""
    original = _svc._plugin

    try:
        with set_claude_code_plugin_for_tests(ClaudeCodePlugin()):
            raise RuntimeError("intentional")
    except RuntimeError:
        pass

    assert _svc._plugin is original


def test_set_claude_code_plugin_for_tests_default_creates_fresh():
    """Omitting the argument creates a fresh ClaudeCodePlugin instance."""
    original = _svc._plugin
    with set_claude_code_plugin_for_tests() as got:
        assert isinstance(got, ClaudeCodePlugin)
        assert got is not original
