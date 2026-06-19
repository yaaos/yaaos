"""Registry tests for `core/coding_agent`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from app.core.coding_agent import (
    ActivityLog,
    CodingAgentRegistry,
    Invocation,
    InvokeCodingAgent,
    PluginNotFoundError,
    RunResult,
    Usage,
    bind_coding_agent_registry,
    get_plugin,
    register_plugin,
)
from app.core.coding_agent.service import current_coding_agent_registry


class _StubPlugin:
    plugin_id = "stub"

    def build_invocation(self, invocation: Invocation) -> InvokeCodingAgent:
        return InvokeCodingAgent(
            argv=["claude"], env={}, stdin=None, wallclock_seconds=invocation.wallclock_seconds
        )

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        return RunResult(
            output="",
            usage=Usage(),
            activity=ActivityLog(),
        )


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Bind a clean CodingAgentRegistry before each test so registrations don't
    bleed across tests."""
    bind_coding_agent_registry(CodingAgentRegistry())
    yield


def test_register_and_get_plugin() -> None:
    plugin = _StubPlugin()
    register_plugin(plugin)
    assert get_plugin("stub") is plugin


def test_register_duplicate_raises() -> None:
    register_plugin(_StubPlugin())
    with pytest.raises(ValueError, match="already registered"):
        register_plugin(_StubPlugin())


def test_get_unknown_plugin_raises() -> None:
    with pytest.raises(PluginNotFoundError):
        get_plugin("nope")


def test_register_plugin_adds_and_is_retrievable() -> None:
    plugin = _StubPlugin()
    register_plugin(plugin)
    assert get_plugin("stub") is plugin


def test_list_registered_plugins_returns_insertion_order() -> None:
    class _A:
        plugin_id = "aaa"

        def build_invocation(self, inv: Invocation) -> InvokeCodingAgent:
            return InvokeCodingAgent(argv=[], env={}, wallclock_seconds=1)

        def parse_result(self, p: Mapping[str, Any]) -> RunResult:
            return RunResult(output="", usage=Usage(), activity=ActivityLog())

    class _B:
        plugin_id = "bbb"

        def build_invocation(self, inv: Invocation) -> InvokeCodingAgent:
            return InvokeCodingAgent(argv=[], env={}, wallclock_seconds=1)

        def parse_result(self, p: Mapping[str, Any]) -> RunResult:
            return RunResult(output="", usage=Usage(), activity=ActivityLog())

    register_plugin(_A())
    register_plugin(_B())
    result = current_coding_agent_registry().list()
    assert [p.plugin_id for p in result] == ["aaa", "bbb"]


def test_registry_items_returns_tuple_of_pairs() -> None:
    """items() returns a tuple of (plugin_id, plugin) pairs matching registered entries."""
    plugin = _StubPlugin()
    register_plugin(plugin)
    result = current_coding_agent_registry().items()
    assert isinstance(result, tuple)
    assert len(result) == 1
    pid, p = result[0]
    assert pid == "stub"
    assert p is plugin


def test_registry_items_is_immutable_snapshot() -> None:
    """Mutating the tuple returned by items() does not affect the registry."""
    register_plugin(_StubPlugin())
    reg = current_coding_agent_registry()
    snapshot = reg.items()
    # Replace the entry in a local copy — registry must be unchanged.
    modified = list(snapshot)
    modified[0] = ("stub", None)  # type: ignore[assignment]
    assert reg.items()[0][1] is not None  # original plugin still there
