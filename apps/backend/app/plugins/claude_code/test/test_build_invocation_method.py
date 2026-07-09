"""`ClaudeCodePlugin.compile_invocation` — translates a high-level `Invocation` into a concrete exec block.

Pure-unit: no DB, no IO.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.coding_agent import CodingAgentError, Invocation, InvokeCodingAgent
from app.plugins.claude_code.service import ClaudeCodePlugin

_STUB_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000099")


def _plugin() -> ClaudeCodePlugin:
    return ClaudeCodePlugin()


def _stage_invocation(**overrides) -> Invocation:  # type: ignore[no-untyped-def]
    base: dict = {
        "workspace_id": _STUB_WORKSPACE_ID,
        "skill": "requirements",
        "model": "opus",
        "effort": "medium",
        "context": {
            "ticket_id": "00000000-0000-0000-0000-0000000000aa",
            "stage_name": "requirements",
            "branch_name": "yaaos/feature-abc123",
            "input": "Add a widget.",
            "artifact_path": "$TMPDIR/00000000-0000-0000-0000-0000000000bb.md",
            "output_schema": {"type": "object", "properties": {"outcome": {"type": "string"}}},
        },
        "wallclock_seconds": 300,
    }
    base.update(overrides)
    return Invocation(**base)


def test_returns_invoke_coding_agent_instance() -> None:
    result = _plugin().compile_invocation(_stage_invocation())
    assert isinstance(result, InvokeCodingAgent)


def test_argv_non_empty() -> None:
    result = _plugin().compile_invocation(_stage_invocation())
    assert len(result.argv) > 0
    assert result.argv[0] == "claude"


def test_argv_contains_model_and_effort() -> None:
    result = _plugin().compile_invocation(_stage_invocation(model="sonnet", effort="high"))
    argv = result.argv
    i = argv.index("--model")
    assert argv[i + 1] == "sonnet"
    j = argv.index("--effort")
    assert argv[j + 1] == "high"


def test_wallclock_seconds_propagated() -> None:
    result = _plugin().compile_invocation(_stage_invocation(wallclock_seconds=600))
    assert result.wallclock_seconds == 600


def test_env_does_not_carry_anthropic_api_key() -> None:
    """ANTHROPIC_API_KEY is never in InvokeCodingAgent.env — it is delivered via
    ConfigUpdate.api_keys and injected by the agent at exec time."""
    result = _plugin().compile_invocation(_stage_invocation())
    assert "ANTHROPIC_API_KEY" not in result.env


def test_env_is_empty_dict() -> None:
    """compile_invocation always produces an empty env dict — no secrets inline."""
    result = _plugin().compile_invocation(_stage_invocation())
    assert result.env == {}


def test_any_skill_name_compiles() -> None:
    """No hardcoded skill allowlist — the named skill file (resolved and
    stat'd agent-side) is the only gate on what skill can run."""
    result = _plugin().compile_invocation(_stage_invocation(skill="code-review"))
    assert 'Use the "code-review" skill' in (result.stdin or "")


def test_missing_required_context_key_raises() -> None:
    inv = _stage_invocation()
    ctx = dict(inv.context)
    del ctx["artifact_path"]
    with pytest.raises(CodingAgentError, match="artifact_path"):
        _plugin().compile_invocation(Invocation(**{**inv.model_dump(), "context": ctx}))


def test_prompt_includes_input_and_artifact_path() -> None:
    result = _plugin().compile_invocation(_stage_invocation())
    assert "Add a widget." in (result.stdin or "")
    assert "$TMPDIR/00000000-0000-0000-0000-0000000000bb.md" in (result.stdin or "")


def test_prompt_includes_pr_context_when_present() -> None:
    inv = _stage_invocation(
        skill="code-review",
        context={
            "ticket_id": "00000000-0000-0000-0000-0000000000aa",
            "stage_name": "code-review",
            "branch_name": "yaaos/feature-abc123",
            "input": "Review the diff.",
            "artifact_path": "$TMPDIR/cmd.md",
            "output_schema": {},
            "pr": {
                "pr_external_id": "acme/web#42",
                "head_sha": "deadbeef",
                "base_sha": "cafebabe",
                "prev_reviewed_head_sha": None,
            },
        },
    )
    result = _plugin().compile_invocation(inv)
    stdin = result.stdin or ""
    assert "acme/web#42" in stdin
    assert "git diff cafebabe..deadbeef" in stdin


def test_prompt_includes_strict_json_instruction() -> None:
    result = _plugin().compile_invocation(_stage_invocation())
    stdin = result.stdin or ""
    assert "Respond with EXACTLY a JSON object" in stdin


def test_argv_includes_stream_json_flag() -> None:
    result = _plugin().compile_invocation(_stage_invocation())
    assert "--output-format=stream-json" in result.argv


def test_argv_includes_bypass_permissions() -> None:
    result = _plugin().compile_invocation(_stage_invocation())
    assert "--permission-mode=bypassPermissions" in result.argv
