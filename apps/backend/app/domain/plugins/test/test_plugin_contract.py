"""Each shipped plugin (github, claude_code, in_process) exposes the M03
contract methods: `install_url(org_id) -> str | None`, `validate_settings(d) -> d`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.coding_agent import _PLUGINS as _CODING_AGENT_PLUGINS
from app.domain.vcs.registry import _PLUGINS as _VCS_PLUGINS


@pytest.fixture(autouse=True)
def _ensure_plugins_registered() -> None:
    """Re-register plugins if a prior test cleared the registries."""
    from app.plugins.claude_code.service import bootstrap as _cc_bootstrap  # noqa: PLC0415
    from app.plugins.github.service import bootstrap as _gh_bootstrap  # noqa: PLC0415

    if "claude_code" not in _CODING_AGENT_PLUGINS:
        _cc_bootstrap()
    if "github" not in _VCS_PLUGINS:
        _gh_bootstrap()


def test_github_install_url_is_relative_path() -> None:
    plugin = _VCS_PLUGINS["github"]
    url = plugin.install_url(uuid4())
    assert url == "/api/github/install"


def test_github_validate_settings_accepts_empty_and_installation_id() -> None:
    plugin = _VCS_PLUGINS["github"]
    assert plugin.validate_settings({}) == {}
    assert plugin.validate_settings({"installation_id": 12345}) == {"installation_id": 12345}


def test_github_validate_settings_rejects_unknown_keys() -> None:
    from app.domain.vcs import VCSValidationError  # noqa: PLC0415

    plugin = _VCS_PLUGINS["github"]
    with pytest.raises(VCSValidationError):
        plugin.validate_settings({"installation_id": 1, "rogue": "value"})


def test_claude_code_install_url_is_none() -> None:
    plugin = _CODING_AGENT_PLUGINS["claude_code"]
    # Stub wrapper may be active in test mode; unwrap to get the real plugin's value.
    real = getattr(plugin, "_wrapped", plugin)
    assert real.install_url(uuid4()) is None


def test_claude_code_validate_settings_accepts_known_shape() -> None:
    plugin = _CODING_AGENT_PLUGINS["claude_code"]
    real = getattr(plugin, "_wrapped", plugin)
    result = real.validate_settings({"orchestrator": {}, "agents": []})
    assert result == {"orchestrator": {}, "agents": []}


def test_claude_code_validate_settings_rejects_unknown_keys() -> None:
    plugin = _CODING_AGENT_PLUGINS["claude_code"]
    real = getattr(plugin, "_wrapped", plugin)
    with pytest.raises(ValueError, match="unknown claude_code"):
        real.validate_settings({"rogue": 1})
