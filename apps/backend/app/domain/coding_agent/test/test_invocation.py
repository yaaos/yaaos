"""`build_invocation` — shape of `InvokeClaudeCodeCommand.invocation`.

The dict crosses the wire to the Go agent which unmarshals it into its
per-mode struct. The Python side is the schema owner; this test pins the
shape so an accidental change shows up loudly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from app.domain.coding_agent import (
    AnswerQuestionContext,
    FindingAnchor,
    InvocationMode,
    ReviewContext,
    build_invocation,
)
from app.domain.vcs import Diff, VCSPullRequest


def _ctx() -> AnswerQuestionContext:
    return AnswerQuestionContext(
        original_finding_title="t",
        original_finding_body="b",
        original_rule_id="r1",
        code_snippet="def x(): return None",
        current_anchor=FindingAnchor(file_path="src/foo.py", line_start=1, line_end=1),
        question="why?",
        head_sha="deadbeef",
    )


def test_shape_has_mode_context_prompt_config() -> None:
    inv = build_invocation(mode="answer_question", context=_ctx())
    assert inv["mode"] == "answer_question"
    assert isinstance(inv["context"], dict)
    assert inv["context"]["question"] == "why?"
    assert inv["prompt_config"] == {"model": "opus", "effort": "medium"}


def test_overrides_replace_defaults() -> None:
    inv = build_invocation(mode="answer_question", context=_ctx(), model="sonnet", effort="high")
    assert inv["prompt_config"] == {"model": "sonnet", "effort": "high"}


def test_context_is_json_serializable() -> None:
    """The wire layer Marshals the dict to JSON via the outbox. The
    context dict must therefore round-trip cleanly. Use model_dump's
    mode='json' which serializes UUIDs / datetimes."""
    inv = build_invocation(mode="answer_question", context=_ctx())
    # No exception → serializable.
    encoded = json.dumps(inv)
    decoded = json.loads(encoded)
    assert decoded["mode"] == "answer_question"


def test_non_pydantic_context_rejected() -> None:
    with pytest.raises(TypeError, match="must be a Pydantic BaseModel"):
        build_invocation(mode="answer_question", context={"hello": "world"})  # type: ignore[arg-type]


def test_all_five_modes_typecheck() -> None:
    """Just a literal-domain sanity check — the Literal type catches
    typos at type-check time; this runs at runtime to catch a missing
    mode in test_aggregate-style refactors."""
    for mode in ("review", "incremental_review", "verify_fix", "stale_check", "answer_question"):
        m: InvocationMode = mode  # type: ignore[assignment]
        del m


# ── exec block shape (slice 72) ─────────────────────────────────────────


def test_exec_block_has_argv_stdin_env() -> None:
    """`exec` is what the Go agent reads to spawn Claude Code. It must
    carry the three fields `RunStreaming` expects + nothing else
    (forward-compat — Go side decodes strict)."""
    inv = build_invocation(mode="answer_question", context=_ctx(), anthropic_api_key=SecretStr("sk-test"))
    assert set(inv["exec"].keys()) == {"argv", "stdin", "env"}
    assert isinstance(inv["exec"]["argv"], list)
    assert isinstance(inv["exec"]["stdin"], str)
    assert isinstance(inv["exec"]["env"], dict)


def test_exec_argv_starts_with_claude_print_streamjson() -> None:
    inv = build_invocation(mode="answer_question", context=_ctx(), anthropic_api_key=SecretStr("sk-test"))
    argv = inv["exec"]["argv"]
    assert argv[0] == "claude"
    assert "--print" in argv
    assert "--output-format=stream-json" in argv
    assert "--verbose" in argv
    # Model + effort flags carry the prompt_config values.
    i = argv.index("--model")
    assert argv[i + 1] == "opus"
    i = argv.index("--effort")
    assert argv[i + 1] == "medium"


def test_exec_argv_model_effort_overrides_propagate() -> None:
    inv = build_invocation(
        mode="answer_question",
        context=_ctx(),
        model="sonnet",
        effort="high",
        anthropic_api_key=SecretStr("sk"),
    )
    argv = inv["exec"]["argv"]
    i = argv.index("--model")
    assert argv[i + 1] == "sonnet"
    i = argv.index("--effort")
    assert argv[i + 1] == "high"


def test_exec_stdin_contains_prompt_and_schema_appendix() -> None:
    """The rendered prompt is the question template + the schema footer.
    Don't assert on the full text (the templates are versioned files we
    edit independently) — just check the two markers we know must be
    present for the agent to respond correctly."""
    inv = build_invocation(mode="answer_question", context=_ctx(), anthropic_api_key=SecretStr("sk"))
    stdin = inv["exec"]["stdin"]
    # The schema-appendix STRICT footer is the agent's response contract.
    assert "Output Format (STRICT)" in stdin
    # The context's question gets rendered into the prompt.
    assert "why?" in stdin


def test_exec_env_carries_anthropic_api_key() -> None:
    inv = build_invocation(
        mode="answer_question", context=_ctx(), anthropic_api_key=SecretStr("sk-secret-abc")
    )
    assert inv["exec"]["env"] == {"ANTHROPIC_API_KEY": "sk-secret-abc"}


def test_exec_env_empty_when_no_key_supplied() -> None:
    """In-process plugin callers can omit the key (they load it from
    settings themselves). Remote-agent callers MUST supply it or the
    spawned `claude` won't authenticate — that's a caller-side check."""
    inv = build_invocation(mode="answer_question", context=_ctx())
    assert inv["exec"]["env"] == {}


def test_exec_answer_question_uses_leaner_allowed_tools() -> None:
    """answer_question drops `Task` from allowed-tools — the parent
    answers directly, no subagent dispatch."""
    inv = build_invocation(mode="answer_question", context=_ctx(), anthropic_api_key=SecretStr("sk"))
    argv = inv["exec"]["argv"]
    tool_flag = next(a for a in argv if a.startswith("--allowed-tools="))
    assert "Task" not in tool_flag


def test_exec_review_uses_full_allowed_tools() -> None:
    """Full review keeps `Task` so the parent can dispatch yaaos-* subagents."""
    pr = VCSPullRequest(
        plugin_id="github",
        external_id="acme/web#1",
        repo_external_id="acme/web",
        number=1,
        title="t",
        body="b",
        author_login="dev",
        author_type="user",
        base_branch="main",
        head_branch="feature",
        base_sha="b",
        head_sha="h",
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="https://x",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ctx = ReviewContext(pr=pr, diff=Diff(raw="", files=[]), lessons=[])
    inv = build_invocation(mode="review", context=ctx, anthropic_api_key=SecretStr("sk"))
    tool_flag = next(a for a in inv["exec"]["argv"] if a.startswith("--allowed-tools="))
    assert "Task" in tool_flag
