"""`CodexPlugin.validate_settings` — validates and normalizes the settings dict.

Pure-unit: no DB, no IO.
"""

from __future__ import annotations

import pytest

from app.plugins.codex.service import CodexPlugin


def _plugin() -> CodexPlugin:
    return CodexPlugin()


def test_api_key_mode_accepted() -> None:
    result = _plugin().validate_settings({"auth_mode": "api_key"})
    assert result == {"auth_mode": "api_key"}


def test_per_user_mode_accepted() -> None:
    result = _plugin().validate_settings({"auth_mode": "per_user"})
    assert result == {"auth_mode": "per_user"}


def test_empty_settings_defaults_to_api_key() -> None:
    result = _plugin().validate_settings({})
    assert result == {"auth_mode": "api_key"}


def test_invalid_auth_mode_raises() -> None:
    with pytest.raises(ValueError):
        _plugin().validate_settings({"auth_mode": "oauth"})


def test_explicit_none_auth_mode_raises() -> None:
    with pytest.raises(ValueError):
        _plugin().validate_settings({"auth_mode": None})


def test_unknown_key_raises() -> None:
    with pytest.raises(ValueError):
        _plugin().validate_settings({"auth_mode": "api_key", "extra_field": "oops"})
