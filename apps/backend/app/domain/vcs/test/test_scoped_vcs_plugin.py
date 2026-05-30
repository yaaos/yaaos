"""scoped_vcs_plugin context manager — isolated-registration contract."""

from __future__ import annotations

import pytest

from app.core.plugin_kit import PluginMeta
from app.domain.vcs import is_registered
from app.domain.vcs.registry import scoped_vcs_plugin


class _FakeVCSPlugin:
    """Minimal VCSPlugin stub — enough for registry tests."""

    def __init__(self, plugin_id: str = "test-github") -> None:
        self.meta = PluginMeta(id=plugin_id, type="vcs", display_name=f"fake-{plugin_id}")


def test_scoped_vcs_plugin_registers_while_inside() -> None:
    """Plugin is findable inside the block."""
    plugin = _FakeVCSPlugin("scoped-vcs-test")

    with scoped_vcs_plugin(plugin):  # type: ignore[arg-type]
        assert is_registered("scoped-vcs-test")


def test_scoped_vcs_plugin_unregisters_after_exit() -> None:
    """Plugin is gone once the block exits normally."""
    plugin = _FakeVCSPlugin("scoped-vcs-exit")

    with scoped_vcs_plugin(plugin):  # type: ignore[arg-type]
        pass

    assert not is_registered("scoped-vcs-exit")


def test_scoped_vcs_plugin_unregisters_on_exception() -> None:
    """Plugin is unregistered even when an exception propagates."""
    plugin = _FakeVCSPlugin("scoped-vcs-exc")

    with pytest.raises(RuntimeError, match="test-error"):
        with scoped_vcs_plugin(plugin):  # type: ignore[arg-type]
            raise RuntimeError("test-error")

    assert not is_registered("scoped-vcs-exc")


def test_scoped_vcs_plugin_yields_plugin() -> None:
    """The context manager yields the same plugin object it received."""
    plugin = _FakeVCSPlugin("scoped-vcs-yield")

    with scoped_vcs_plugin(plugin) as p:  # type: ignore[arg-type]
        assert p is plugin
