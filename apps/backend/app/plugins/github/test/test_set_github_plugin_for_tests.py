"""Tests for set_github_plugin_for_tests isolation seam."""

import app.plugins.github.service as _svc
from app.plugins.github.service import GitHubPlugin, set_github_plugin_for_tests


def test_set_github_plugin_for_tests_swaps_for_block():
    """Inside the block the singleton is replaced with the provided instance."""
    replacement = GitHubPlugin()
    with set_github_plugin_for_tests(replacement) as got:
        assert got is replacement
        assert _svc._plugin is replacement


def test_set_github_plugin_for_tests_restores_after_exit():
    """The original singleton is restored after block exit."""
    original = _svc._plugin
    replacement = GitHubPlugin()

    with set_github_plugin_for_tests(replacement):
        assert _svc._plugin is replacement

    assert _svc._plugin is original


def test_set_github_plugin_for_tests_restores_on_exception():
    """The original singleton is restored even when the block raises."""
    original = _svc._plugin

    try:
        with set_github_plugin_for_tests(GitHubPlugin()):
            raise RuntimeError("intentional")
    except RuntimeError:
        pass

    assert _svc._plugin is original


def test_set_github_plugin_for_tests_default_creates_fresh():
    """Omitting the argument creates a fresh GitHubPlugin instance."""
    original = _svc._plugin
    with set_github_plugin_for_tests() as got:
        assert isinstance(got, GitHubPlugin)
        assert got is not original
