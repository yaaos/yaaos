"""Stub wrapper tests — no DB, no subprocess, no env."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from app.core.coding_agent import (
    ActivityLog,
    CodingAgentRegistry,
    Invocation,
    InvokeCodingAgent,
    RunResult,
    Usage,
    bind_coding_agent_registry,
    current_coding_agent_registry,
    register_plugin,
)
from app.testing.stub_coding_agent import (
    StubCodingAgentPlugin,
    wrap_all_registered_plugins,
)


class _DummyPlugin:
    plugin_id = "dummy"

    def compile_invocation(self, invocation: Invocation) -> InvokeCodingAgent:
        return InvokeCodingAgent(
            argv=["real-claude"],
            env={},
            stdin=None,
            wallclock_seconds=invocation.wallclock_seconds,
        )

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        return RunResult(output="real", usage=Usage(), activity=ActivityLog())


def test_compile_invocation_returns_stub_argv() -> None:
    """StubCodingAgentPlugin.compile_invocation returns a minimal stub exec block."""
    stub = StubCodingAgentPlugin(wrapped=_DummyPlugin())
    inv = Invocation(
        workspace_id=uuid.UUID(int=0),
        skill="pr_review",
        model="opus",
        effort="medium",
        context={},
        wallclock_seconds=60,
    )
    result = stub.compile_invocation(inv)
    assert isinstance(result, InvokeCodingAgent)
    assert result.argv == ["stub"]
    assert result.wallclock_seconds == 60


def test_parse_result_returns_run_result() -> None:
    """StubCodingAgentPlugin.parse_result returns a RunResult with canned tokens."""
    stub = StubCodingAgentPlugin(wrapped=_DummyPlugin())
    result = stub.parse_result({"stdout": "some output", "exit_code": 0})
    assert isinstance(result, RunResult)
    assert result.output == "some output"
    assert result.exit_code == 0
    assert result.usage.tokens_in == 1000
    assert result.usage.tokens_out == 200
    assert result.error_message is None


def test_plugin_id_mirrors_wrapped() -> None:
    stub = StubCodingAgentPlugin(wrapped=_DummyPlugin())
    assert stub.plugin_id == "dummy"


def test_wrap_all_is_idempotent() -> None:
    # Start from an empty registry so the test is independent of suite state.
    bind_coding_agent_registry(CodingAgentRegistry())
    dummy = _DummyPlugin()
    register_plugin(dummy)
    assert wrap_all_registered_plugins() == 1
    plugins = current_coding_agent_registry().list()
    assert len(plugins) == 1
    assert isinstance(plugins[0], StubCodingAgentPlugin)
    # second call is a no-op — already wrapped
    assert wrap_all_registered_plugins() == 0
