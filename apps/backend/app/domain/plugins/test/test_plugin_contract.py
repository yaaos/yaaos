"""Each shipped plugin (github, claude_code) exposes the contract methods: `install_url(org_id) -> str | None`, `validate_settings(d) -> d`."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.coding_agent import get_plugin as get_coding_agent_plugin
from app.domain.coding_agent import registered_plugin_ids
from app.domain.vcs import get_plugin as get_vcs_plugin
from app.domain.vcs import is_registered as vcs_is_registered


@pytest.fixture(autouse=True)
def _ensure_plugins_registered() -> None:
    """Re-register plugins if a prior test cleared the registries."""
    from app.plugins.claude_code import bootstrap as _cc_bootstrap  # noqa: PLC0415
    from app.plugins.github import bootstrap as _gh_bootstrap  # noqa: PLC0415

    if "claude_code" not in registered_plugin_ids():
        _cc_bootstrap()
    if not vcs_is_registered("github"):
        _gh_bootstrap()


def test_github_install_url_is_none() -> None:
    """The github plugin's install handshake is driven by a dedicated
    `POST /api/github/install/start` endpoint that signs state, not the
    `install_url(org_id)` contract method (which is for browser-redirect-only
    installs). Returning None here keeps `POST /api/vcs` from short-circuiting
    on github."""
    plugin = get_vcs_plugin("github")
    assert plugin.install_url(uuid4()) is None


def test_github_validate_settings_accepts_empty_and_installation_id() -> None:
    plugin = get_vcs_plugin("github")
    assert plugin.validate_settings({}) == {}
    assert plugin.validate_settings({"installation_id": 12345}) == {"installation_id": 12345}


def test_github_validate_settings_rejects_unknown_keys() -> None:
    from app.domain.vcs import VCSValidationError  # noqa: PLC0415

    plugin = get_vcs_plugin("github")
    with pytest.raises(VCSValidationError):
        plugin.validate_settings({"installation_id": 1, "rogue": "value"})


def test_claude_code_install_url_is_none() -> None:
    plugin = get_coding_agent_plugin("claude_code")
    # Stub wrapper may be active in test mode; unwrap to get the real plugin's value.
    real = getattr(plugin, "_wrapped", plugin)
    assert real.install_url(uuid4()) is None


def test_claude_code_validate_settings_substitutes_defaults_on_empty() -> None:
    """The picker's POST /api/coding-agents path calls validate_settings({});
    fills in the code defaults so the install row starts populated."""
    plugin = get_coding_agent_plugin("claude_code")
    real = getattr(plugin, "_wrapped", plugin)
    result = real.validate_settings({})
    assert result["orchestrator"]["name"] == "Orchestrator"
    assert len(result["agents"]) >= 1


def test_claude_code_validate_settings_rejects_unknown_keys() -> None:
    plugin = get_coding_agent_plugin("claude_code")
    real = getattr(plugin, "_wrapped", plugin)
    with pytest.raises(ValueError):
        real.validate_settings({"rogue": 1, "orchestrator": {}, "agents": []})
