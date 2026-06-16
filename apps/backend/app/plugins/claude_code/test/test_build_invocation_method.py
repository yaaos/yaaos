"""`ClaudeCodePlugin.build_invocation` — translates a high-level `Invocation` into a concrete exec block.

Pure-unit: no DB, no IO.
"""

from __future__ import annotations

import pytest

from app.core.coding_agent import CodingAgentError, Invocation, InvokeCodingAgent
from app.plugins.claude_code.service import ClaudeCodePlugin


def _plugin() -> ClaudeCodePlugin:
    return ClaudeCodePlugin()


def _pr_review_invocation(**overrides) -> Invocation:  # type: ignore[no-untyped-def]
    base: dict = {
        "skill": "pr_review",
        "model": "opus",
        "effort": "medium",
        "context": {
            "org_id": "00000000-0000-0000-0000-000000000001",
            "repo_external_id": "acme/web",
            "pr_external_id": "acme/web#42",
            "head_sha": "deadbeef",
            "base_sha": "cafebabe",
            "anthropic_api_key": "sk-test-key",
        },
        "wallclock_seconds": 300,
    }
    base.update(overrides)
    return Invocation(**base)


def test_returns_invoke_coding_agent_instance() -> None:
    result = _plugin().build_invocation(_pr_review_invocation())
    assert isinstance(result, InvokeCodingAgent)


def test_argv_non_empty() -> None:
    result = _plugin().build_invocation(_pr_review_invocation())
    assert len(result.argv) > 0
    assert result.argv[0] == "claude"


def test_argv_contains_model_and_effort() -> None:
    result = _plugin().build_invocation(_pr_review_invocation(model="sonnet", effort="high"))
    argv = result.argv
    i = argv.index("--model")
    assert argv[i + 1] == "sonnet"
    j = argv.index("--effort")
    assert argv[j + 1] == "high"


def test_wallclock_seconds_propagated() -> None:
    result = _plugin().build_invocation(_pr_review_invocation(wallclock_seconds=600))
    assert result.wallclock_seconds == 600


def test_env_carries_api_key_when_supplied() -> None:
    result = _plugin().build_invocation(_pr_review_invocation())
    assert result.env.get("ANTHROPIC_API_KEY") == "sk-test-key"


def test_env_empty_when_no_api_key_in_context() -> None:
    inv = _pr_review_invocation()
    ctx = dict(inv.context)
    ctx.pop("anthropic_api_key")
    result = _plugin().build_invocation(Invocation(**{**inv.model_dump(), "context": ctx}))
    assert "ANTHROPIC_API_KEY" not in result.env


def test_unknown_skill_raises_coding_agent_error() -> None:
    inv = _pr_review_invocation(skill="unknown_skill")
    with pytest.raises(CodingAgentError, match="unknown_skill"):
        _plugin().build_invocation(inv)


def test_missing_required_context_key_raises() -> None:
    inv = _pr_review_invocation()
    ctx = dict(inv.context)
    del ctx["head_sha"]
    with pytest.raises(CodingAgentError, match="head_sha"):
        _plugin().build_invocation(Invocation(**{**inv.model_dump(), "context": ctx}))


def test_argv_includes_stream_json_flag() -> None:
    result = _plugin().build_invocation(_pr_review_invocation())
    assert "--output-format=stream-json" in result.argv


def test_argv_includes_bypass_permissions() -> None:
    result = _plugin().build_invocation(_pr_review_invocation())
    assert "--permission-mode=bypassPermissions" in result.argv
