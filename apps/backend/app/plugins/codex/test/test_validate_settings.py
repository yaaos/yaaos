"""`CodexPlugin.validate_settings` — validates a raw settings dict.

Pure-unit: no DB, no IO. Codex has no per-org auth setting — the only
credential source is the org's OpenAI API key (managed via `core/api_keys`).
"""

from __future__ import annotations

import pytest

from app.plugins.codex.service import CodexPlugin


def _plugin() -> CodexPlugin:
    return CodexPlugin()


def test_empty_settings_accepted() -> None:
    result = _plugin().validate_settings({})
    assert result == {}


def test_unknown_key_raises() -> None:
    with pytest.raises(ValueError):
        _plugin().validate_settings({"extra_field": "oops"})


def test_auth_mode_key_raises_as_unknown() -> None:
    """auth_mode is no longer a recognized key — it's rejected like any other."""
    with pytest.raises(ValueError):
        _plugin().validate_settings({"auth_mode": "api_key"})
