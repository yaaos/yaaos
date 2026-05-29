"""scoped_coding_agent context manager — isolated-registration contract."""

from __future__ import annotations

import pytest

from app.domain.coding_agent import (
    get_plugin,
    registered_plugin_ids,
)
from app.domain.coding_agent.service import scoped_coding_agent
from app.testing.fake_coding_agent import FakeCodingAgentPlugin


def test_scoped_coding_agent_registers_while_inside() -> None:
    """Plugin is findable inside the block."""
    plugin = FakeCodingAgentPlugin(plugin_id="scoped-agent-test")

    with scoped_coding_agent(plugin):  # type: ignore[arg-type]
        assert "scoped-agent-test" in registered_plugin_ids()
        found = get_plugin("scoped-agent-test")
        assert found is plugin


def test_scoped_coding_agent_unregisters_after_exit() -> None:
    """Plugin is gone once the block exits normally."""
    plugin = FakeCodingAgentPlugin(plugin_id="scoped-agent-exit")

    with scoped_coding_agent(plugin):  # type: ignore[arg-type]
        pass

    assert "scoped-agent-exit" not in registered_plugin_ids()


def test_scoped_coding_agent_unregisters_on_exception() -> None:
    """Plugin is unregistered even when an exception propagates."""
    plugin = FakeCodingAgentPlugin(plugin_id="scoped-agent-exc")

    with pytest.raises(RuntimeError, match="test-error"):
        with scoped_coding_agent(plugin):  # type: ignore[arg-type]
            raise RuntimeError("test-error")

    assert "scoped-agent-exc" not in registered_plugin_ids()


def test_scoped_coding_agent_yields_plugin() -> None:
    """The context manager yields the same plugin object it received."""
    plugin = FakeCodingAgentPlugin(plugin_id="scoped-agent-yield")

    with scoped_coding_agent(plugin) as p:  # type: ignore[arg-type]
        assert p is plugin
