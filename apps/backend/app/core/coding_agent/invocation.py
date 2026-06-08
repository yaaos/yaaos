"""Builds the `invocation` block of the `InvokeClaudeCode` AgentCommand
payload for each of the five reviewer task modes.

The wire-layer `InvokeClaudeCodeCommand.invocation` is `dict[str, Any]` —
intentionally permissive because shape ownership is in `core/coding_agent`,
not the wire. This module is that owner.

The Go workspace agent unmarshals the dict, reads the `exec` sub-block
(`argv` + `stdin` + `env`), and spawns the Claude Code subprocess directly
— the agent itself carries no prompt or invocation policy (per the
"WorkspaceAgent has zero biz logic" rule). The control plane builds the
exec shape here.

Wire shape:

    {
        "mode": "review" | "incremental_review" | "verify_fix" | "stale_check" | "answer_question",
        "context": <FooContext.model_dump()>,
        "prompt_config": {
            "model": "opus" | "sonnet",
            "effort": "low" | "medium" | "high",
        },
        "exec": {
            "argv": ["claude", "--print", "--output-format=stream-json", ...],
            "stdin": "<full rendered prompt>",
            "env": {"ANTHROPIC_API_KEY": "..."},
        },
    }

`exec.argv` does NOT include the absolute binary path — the agent resolves
`claude` via PATH (the Dockerfile installs it; uses the `--cli_path`
override on the in-process path only). `exec.env` carries the secrets the
agent merges into the subprocess's environment (`os.Environ()` is
inherited by default; this overrides keys per the `RunStreaming` contract).
`exec.stdin` is the fully-rendered prompt body INCLUDING the schema
appendix — what gets piped as the agent's stdin.

`mode` + `context` + `prompt_config` are retained for observability and
for the eventual Pydantic-codegen path; the agent ignores them in favour
of `exec`. Today the in-process plugin still uses them to drive its
local code path (it doesn't read `exec`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, SecretStr

from app.core.coding_agent.prompts import (
    AnswerQuestionDto,
    FindingDraftList,
    StaleCheckDto,
    VerifyFixDto,
    assemble_answer_question_prompt,
    assemble_incremental_review_prompt,
    assemble_stale_check_prompt,
    assemble_verify_fix_prompt,
    schema_appendix,
)
from app.core.coding_agent.types import (
    AnswerQuestionContext,
    IncrementalReviewContext,
    ReviewContext,
    StaleCheckContext,
    VerifyFixContext,
)

InvocationMode = Literal["review", "incremental_review", "verify_fix", "stale_check", "answer_question"]

# Defaults match `plugins/claude_code`'s `_DEFAULT_MODEL` / `_DEFAULT_EFFORT`.
# Kept here so callers don't need to import the plugin (Tach layering).
_DEFAULT_MODEL = "opus"
_DEFAULT_EFFORT = "medium"

# Default tool allowlist for the full-review parent — restricts the
# agent to read-only repo inspection + git history + Task subagent
# dispatch. Other modes pass `allowed_tools_override` via build_invocation's
# kwarg to use a leaner set (e.g. answer_question turns off Task).
_DEFAULT_ALLOWED_TOOLS = (
    "Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch,Task,"
    "Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),"
    "Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status)"
)

# answer_question runs read-only with no Task subagent dispatch — the
# parent generates the answer directly.
_ANSWER_QUESTION_ALLOWED_TOOLS = (
    "Read,Glob,Grep,LS,NotebookRead,WebFetch,WebSearch,"
    "Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),"
    "Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status)"
)


# The five typed contexts the build_invocation function accepts. Each
# command body picks the one that matches its `mode`.
_Context = (
    ReviewContext | IncrementalReviewContext | VerifyFixContext | StaleCheckContext | AnswerQuestionContext
)


def _exec_block(
    *,
    mode: InvocationMode,
    context: _Context,
    model: str,
    effort: str,
    anthropic_api_key: SecretStr | None,
) -> dict[str, Any]:
    """Render the prompt + assemble argv/env/stdin for the Claude Code
    subprocess. Pure function — no I/O, no DB reads. The agent reads the
    returned dict and exec's it via `workspace.RunStreaming`.

    Caller supplies `anthropic_api_key` (loaded from `claude_code_settings`
    or per-org config). Empty key produces an exec block with no API key
    in `env` — the subprocess will fail to authenticate; that's the
    backend caller's signal to surface the missing-credentials error
    before dispatching.
    """
    stdin, allowed_tools = _render(mode, context)
    argv = [
        "claude",
        "--print",
        "--output-format=stream-json",
        "--verbose",
        "--permission-mode=bypassPermissions",
        "--model",
        model,
        "--effort",
        effort,
        f"--allowed-tools={allowed_tools}",
    ]
    env: dict[str, str] = {}
    if anthropic_api_key is not None:
        # Byte-boundary unwrap: this dict is JSON-serialized into the
        # WorkspaceAgent payload, which sets it as a subprocess env var.
        env["ANTHROPIC_API_KEY"] = anthropic_api_key.get_secret_value()
    return {"argv": argv, "stdin": stdin, "env": env}


def _render(mode: InvocationMode, context: _Context) -> tuple[str, str]:
    """Dispatch over the 5 modes — returns (rendered_prompt, allowed_tools).
    Each mode pairs its prompt assembler with the schema appendix that
    matches the expected response DTO."""
    if mode == "review":
        # The review path now uses plugin.build_review_invocation (Shape B).
        # build_invocation("review", ...) is retained for tests that verify
        # the allowed-tools constant; it no longer generates a prompt body.
        assert isinstance(context, ReviewContext)
        dummy_prompt = (
            f"Review PR {context.pr_external_id} in {context.repo_external_id}. "
            f"Base: {context.base_sha}, Head: {context.head_sha}."
        )
        return dummy_prompt + schema_appendix(FindingDraftList), _DEFAULT_ALLOWED_TOOLS
    if mode == "incremental_review":
        assert isinstance(context, IncrementalReviewContext)
        return (
            assemble_incremental_review_prompt(context) + schema_appendix(FindingDraftList),
            _DEFAULT_ALLOWED_TOOLS,
        )
    if mode == "verify_fix":
        assert isinstance(context, VerifyFixContext)
        return assemble_verify_fix_prompt(context) + schema_appendix(VerifyFixDto), _DEFAULT_ALLOWED_TOOLS
    if mode == "stale_check":
        assert isinstance(context, StaleCheckContext)
        return assemble_stale_check_prompt(context) + schema_appendix(StaleCheckDto), _DEFAULT_ALLOWED_TOOLS
    if mode == "answer_question":
        assert isinstance(context, AnswerQuestionContext)
        return (
            assemble_answer_question_prompt(context) + schema_appendix(AnswerQuestionDto),
            _ANSWER_QUESTION_ALLOWED_TOOLS,
        )
    raise ValueError(f"unknown mode: {mode}")


def build_invocation(
    *,
    mode: InvocationMode,
    context: _Context,
    model: str | None = None,
    effort: str | None = None,
    anthropic_api_key: SecretStr | None = None,
) -> dict[str, Any]:
    """Build the `invocation` dict for `InvokeClaudeCodeCommand.invocation`.

    `context`: one of the five typed `<Mode>Context` Pydantic models. Its
    `.model_dump(mode="json")` is what crosses the wire — must already be
    JSON-serializable end-to-end (the types enforce this).

    `model` / `effort`: override the per-org defaults when the caller has
    org-specific config; else falls through to the constants matching the
    `plugins/claude_code` defaults.

    `anthropic_api_key`: the secret the Go agent needs to set as
    ANTHROPIC_API_KEY in the subprocess env. Optional only because the
    in-process plugin path (which still uses its own settings load) can
    omit it; remote-agent callers MUST supply it or the spawned `claude`
    will fail to authenticate.
    """
    if not isinstance(context, BaseModel):
        raise TypeError(f"context must be a Pydantic BaseModel, got {type(context).__name__}")
    resolved_model = model or _DEFAULT_MODEL
    resolved_effort = effort or _DEFAULT_EFFORT
    return {
        "mode": mode,
        "context": context.model_dump(mode="json"),
        "prompt_config": {
            "model": resolved_model,
            "effort": resolved_effort,
        },
        "exec": _exec_block(
            mode=mode,
            context=context,
            model=resolved_model,
            effort=resolved_effort,
            anthropic_api_key=anthropic_api_key,
        ),
    }


__all__ = ["InvocationMode", "build_invocation"]
