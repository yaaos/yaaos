"""Tests for `configure_gateway` env-var patching."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.core.config import get_settings
from app.core.llm import configure_gateway

_PATCHED_KEYS = (
    "ANTHROPIC_API_BASE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
)


@pytest.fixture
def env_snapshot() -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _PATCHED_KEYS}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _reset_settings_cache() -> None:
    get_settings.cache_clear()


def test_configure_gateway_no_op_when_unset(env_snapshot: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_API_URL", raising=False)
    for k in _PATCHED_KEYS:
        os.environ.pop(k, None)
    _reset_settings_cache()

    configure_gateway()

    for k in _PATCHED_KEYS:
        assert k not in os.environ


def test_configure_gateway_sets_both_providers(env_snapshot: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAINTRUST_API_KEY", "sk-test")
    monkeypatch.setenv("BRAINTRUST_API_URL", "https://gateway.example/v1")
    _reset_settings_cache()

    configure_gateway()

    assert os.environ["ANTHROPIC_API_BASE"] == "https://gateway.example/v1"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test"
    assert os.environ["OPENAI_API_BASE"] == "https://gateway.example/v1"
    assert os.environ["OPENAI_API_KEY"] == "sk-test"
