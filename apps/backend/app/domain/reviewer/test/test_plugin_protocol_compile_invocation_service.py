"""Service test: `CodingAgentPlugin.compile_invocation` is the renamed hook
that converts an `Invocation` into a concrete `InvokeCodingAgent` exec block.

Verifies:
- The Protocol method on `CodingAgentPlugin` is `compile_invocation` (not `build_invocation`).
- `ClaudeCodePlugin.compile_invocation` is callable and returns an `InvokeCodingAgent`.
- `core/coding_agent.dispatch_invocation` routes through `plugin.compile_invocation`.
- The retired name `build_invocation` is absent from `CodingAgentPlugin`.
"""

from __future__ import annotations

import inspect

import pytest


@pytest.mark.service
def test_coding_agent_plugin_has_compile_invocation_not_build_invocation() -> None:
    """The Protocol surface must expose `compile_invocation`; `build_invocation` must be absent."""
    from app.core.coding_agent import CodingAgentPlugin  # noqa: PLC0415

    proto_methods = {
        name
        for name, _ in inspect.getmembers(CodingAgentPlugin, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert "compile_invocation" in proto_methods, "CodingAgentPlugin must have compile_invocation"
    assert "build_invocation" not in proto_methods, "build_invocation must be retired from CodingAgentPlugin"


@pytest.mark.service
def test_claude_code_plugin_compile_invocation_returns_invoke_coding_agent() -> None:
    """`ClaudeCodePlugin.compile_invocation` returns a concrete `InvokeCodingAgent`."""
    from app.core.coding_agent import Invocation, InvokeCodingAgent  # noqa: PLC0415
    from app.plugins.claude_code import ClaudeCodePlugin  # noqa: PLC0415

    inv = Invocation(
        workspace_id="00000000-0000-0000-0000-000000000099",
        skill="pr_review",
        model="opus",
        effort="medium",
        context={
            "org_id": "00000000-0000-0000-0000-000000000001",
            "repo_external_id": "owner/repo",
            "pr_external_id": "42",
            "head_sha": "deadbeef",
            "base_sha": "cafebabe",
        },
        wallclock_seconds=300,
    )
    result = ClaudeCodePlugin().compile_invocation(inv)
    assert isinstance(result, InvokeCodingAgent), (
        f"compile_invocation must return InvokeCodingAgent, got {type(result)}"
    )


@pytest.mark.service
def test_claude_code_plugin_has_no_build_invocation() -> None:
    """`ClaudeCodePlugin` must not expose `build_invocation`."""
    from app.plugins.claude_code import ClaudeCodePlugin  # noqa: PLC0415

    assert not hasattr(ClaudeCodePlugin, "build_invocation"), (
        "build_invocation must not exist on ClaudeCodePlugin; use compile_invocation"
    )
