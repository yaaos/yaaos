"""Prompt assembly + structured-output DTOs for the five reviewer modes.

The per-mode prompt builders live in the domain rather than inside
`plugins/claude_code/service.py` because:

1. `build_invocation` ships the literal `{argv, stdin, env}` exec
   block on the wire so the Go workspace agent can spawn the Claude
   Code subprocess with zero biz logic of its own. To assemble that
   stdin the backend needs the rendered prompt — which means the
   prompt-assembly functions can't sit inside the plugin (Tach: domain
   doesn't import plugins).
2. `plugins/claude_code` is the only renderer, so keeping them in the
   domain is neutral on the in-process path — the plugin imports the
   same names from the domain.

Five `assemble_<mode>_prompt(ctx) -> str` functions + `schema_appendix(
response_model) -> str` (which renders the JSON-schema STRICT footer the
agent's response is expected to match). The four response DTOs
(`FindingDraftList`, `VerifyFixDto`, `StaleCheckDto`, `AnswerQuestionDto`)
live here too — they're the parse targets paired with each prompt.

The `prompts/*.md` template files live next to this module (versioned in
the repo so PR review of prompt changes is direct).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.domain.coding_agent.types import (
    AnswerQuestionContext,
    IncrementalReviewContext,
    ReviewContext,
    StaleCheckContext,
    VerifyFixContext,
)

# ── Prompt-template loader ───────────────────────────────────────────────

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load(name: str) -> str:
    """Read `prompts/{name}.md` at module import. The result is cached by
    Python's import machinery — there's no runtime file I/O after first
    use."""
    return (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


_PARENT_PROMPT_HEADER = _load("full_review")
_INCREMENTAL_PROMPT_HEADER = _load("incremental_review")
_VERIFY_FIX_PROMPT = _load("verify_fix")
_STALE_CHECK_PROMPT = _load("stale_check")
_ANSWER_QUESTION_PROMPT = _load("answer_question")


_MCP_BROKEN_CREDS_ADDENDUM = (
    "If an MCP tool returns `not_connected` or `broken_creds`, note the missing "
    "context in your review and continue."
)


# ── Per-mode prompt assembly ─────────────────────────────────────────────


def assemble_review_prompt(ctx: ReviewContext) -> str:
    """Full-review parent prompt. Includes MCP context block when the
    review has connected providers, language hint, PR metadata, and how
    the parent should pull diffs itself + dispatch yaaos-* subagents.

    The fingerprint dedup at admission (`aggregate.post_process_raw_findings`)
    handles re-emission of the same finding across runs — we deliberately
    do NOT tell the agent to skip prior comments because telling it to
    "not duplicate" was fighting the persistence layer and starved the
    re-observation signal."""
    parts: list[str] = [_PARENT_PROMPT_HEADER, ""]
    mcp = ctx.agent_config.get("mcp") if isinstance(ctx.agent_config, dict) else None
    if mcp and mcp.get("servers"):
        provider_names = sorted(s["provider"] for s in mcp["servers"])
        parts.extend(
            [
                "## MCP context servers",
                "The following MCP servers are connected for this review and may be "
                f"called via the `mcp__<server>__<tool>` toolset: {', '.join(provider_names)}.",
                _MCP_BROKEN_CREDS_ADDENDUM,
                "",
            ]
        )
    if ctx.language_hint:
        parts.extend(
            [
                "## Repository language",
                f"This repository is primarily {ctx.language_hint}.",
                "",
            ]
        )
    parts.extend(
        [
            "## Pull request",
            f"### Title\n{ctx.pr.title}",
            f"### Description\n{ctx.pr.body or '(no description)'}",
            "",
            "## Branch",
            f"- Base: `{ctx.pr.base_branch}` at `{ctx.pr.base_sha}` (the branch this PR will merge into)",
            f"- HEAD: `{ctx.pr.head_branch}` at `{ctx.pr.head_sha}` (currently checked out)",
            "",
            "## How to inspect the changes",
            "Run git commands yourself — the diff is NOT inlined below. You have Bash access "
            "restricted to read-only git commands (`git diff`, `git log`, `git show`, `git blame`, "
            "`git ls-files`, `git rev-parse`, `git status`). Useful starting points:",
            "",
            f"- `git diff {ctx.pr.base_sha}..HEAD --name-only` — list of changed files",
            f"- `git diff {ctx.pr.base_sha}..HEAD --stat` — change summary by file",
            f"- `git diff {ctx.pr.base_sha}..HEAD -- <path>` — diff for one file or directory",
            f"- `git diff {ctx.pr.base_sha}..HEAD` — full diff (use sparingly on large PRs)",
            "",
            "Pass these instructions through to each subagent in its Task brief so the subagent "
            "can pull only the slice of the diff it needs to review.",
        ]
    )
    if ctx.lessons:
        parts.extend(
            [
                "",
                "## Lessons learned from past reviews",
                "Apply these when reviewing this PR. Pass them to each subagent in its task brief.",
                "",
            ]
        )
        for lesson in ctx.lessons:
            parts.append(f"### {lesson.title}\n_lesson_id: {lesson.id}_\n{lesson.body}")
    return "\n".join(parts)


def assemble_incremental_review_prompt(ctx: IncrementalReviewContext) -> str:
    """Incremental review — agent reviews only the `prev_sha..head_sha`
    slice. Prior open + acknowledged findings are listed so the agent
    avoids surface-level duplicates (the aggregate's fingerprint dedup
    still backstops anything that slips through)."""
    parts: list[str] = [
        _INCREMENTAL_PROMPT_HEADER.format(prev_sha=ctx.prev_sha, head_sha=ctx.head_sha),
        "",
        "## Pull request",
        f"### Title\n{ctx.pr.title}",
        f"### Description\n{ctx.pr.body or '(no description)'}",
        "",
        "## Scope",
        f"- prev_sha: `{ctx.prev_sha}`",
        f"- head_sha: `{ctx.head_sha}`",
        f"- base branch: `{ctx.pr.base_branch}`",
        "",
        "## How to inspect the changes",
        f"- `git diff {ctx.prev_sha}..{ctx.head_sha} --name-only`",
        f"- `git diff {ctx.prev_sha}..{ctx.head_sha} --stat`",
        f"- `git diff {ctx.prev_sha}..{ctx.head_sha} -- <path>`",
    ]
    if ctx.language_hint:
        parts.extend(["", "## Repository language", f"This repository is primarily {ctx.language_hint}."])
    if ctx.prior_open_finding_summaries:
        parts.extend(["", "## Prior OPEN findings (do not re-raise — aggregate dedups on fingerprint)"])
        for s in ctx.prior_open_finding_summaries[:30]:
            parts.append(f"- {s[:200]}")
    if ctx.prior_acknowledged_finding_summaries:
        parts.extend(["", "## Prior ACKNOWLEDGED findings (NEVER re-raise — developer explicitly accepted)"])
        for s in ctx.prior_acknowledged_finding_summaries[:30]:
            parts.append(f"- {s[:200]}")
    if ctx.lessons:
        parts.extend(["", "## Lessons learned from past reviews"])
        for lesson in ctx.lessons[:20]:
            parts.append(f"### {lesson.title}\n_lesson_id: {lesson.id}_\n{lesson.body}")
    return "\n".join(parts)


def assemble_verify_fix_prompt(ctx: VerifyFixContext) -> str:
    """Verify-fix subflow prompt. Asks the agent whether the same problem
    is still present at the current anchor location given the original
    finding + the current code snippet."""
    return _VERIFY_FIX_PROMPT.format(
        rule_id=ctx.original_rule_id,
        title=ctx.original_finding_title,
        body=ctx.original_finding_body,
        original_code=ctx.original_code_snippet,
        current_code=ctx.current_code_snippet,
        file_path=ctx.current_anchor.file_path,
        line_start=ctx.current_anchor.line_start,
        line_end=ctx.current_anchor.line_end,
    )


def assemble_stale_check_prompt(ctx: StaleCheckContext) -> str:
    """Stale-check subflow prompt — same shape as verify-fix but the
    question is "does this finding still apply after the diff"."""
    return _STALE_CHECK_PROMPT.format(
        rule_id=ctx.original_rule_id,
        title=ctx.original_finding_title,
        body=ctx.original_finding_body,
        current_code=ctx.current_code_snippet,
        diff_summary=ctx.diff_summary,
    )


def assemble_answer_question_prompt(ctx: AnswerQuestionContext) -> str:
    """Answer-question subflow prompt — asks the agent to write a single
    text reply that the parent commits to posting back to the developer.
    No state transition; no findings."""
    if ctx.prior_messages:
        prior_thread = "\n".join(f"- [{m.author_kind}] {m.body}" for m in ctx.prior_messages)
    else:
        prior_thread = "_(no prior messages — this is the first reply on the finding)_"
    return _ANSWER_QUESTION_PROMPT.format(
        rule_id=ctx.original_rule_id,
        title=ctx.original_finding_title,
        body=ctx.original_finding_body,
        file_path=ctx.current_anchor.file_path,
        line_start=ctx.current_anchor.line_start,
        line_end=ctx.current_anchor.line_end,
        code_snippet=ctx.code_snippet,
        prior_thread=prior_thread,
        question=ctx.question,
        base_sha=ctx.base_sha or "(unknown)",
        head_sha=ctx.head_sha or "(unknown)",
        language_hint=ctx.language_hint or "(unspecified)",
    )


# ── Structured-output schema appendix + DTOs ────────────────────────────


class _FindingDraftDto(BaseModel):
    """The agent's per-finding output shape. Mirrors
    `FindingDraft` field names without depending on the typed import (the
    agent emits JSON; we validate against this then convert). `severity`
    is a closed enum and `confidence` is the 0-100 integer the agent
    emits — the aggregate widens it to a float at admission time."""

    severity: Literal["blocker", "major", "minor", "nit"]
    rule_id: str
    title: str
    body: str
    concrete_failure_scenario: str
    confidence: int
    rationale: str
    file_path: str
    line_start: int
    line_end: int
    duplicate_of_rule_ids: list[str] = []


class FindingDraftList(BaseModel):
    """Full-review + incremental-review response: a flat list of finding
    drafts. The agent is told to respond with `{"findings": [...]}`."""

    findings: list[_FindingDraftDto]


class VerifyFixDto(BaseModel):
    still_present: bool
    confidence: float
    reasoning: str
    observed_line: int | None = None


class StaleCheckDto(BaseModel):
    still_applies: bool
    confidence: float
    reasoning: str


class AnswerQuestionDto(BaseModel):
    """Single text reply the parent agent commits to posting back."""

    answer: str


def schema_appendix(response_model: type[BaseModel]) -> str:
    """STRICT-output footer appended to every prompt — gives the agent the
    JSON schema it must respond with. No markdown fences, no preamble.

    Pairs:
    - full review + incremental → `FindingDraftList`
    - verify-fix → `VerifyFixDto`
    - stale-check → `StaleCheckDto`
    - answer-question → `AnswerQuestionDto`
    """
    return (
        "\n\n## Output Format (STRICT)\n\n"
        "Respond with EXACTLY a JSON object matching this schema. No markdown fences. "
        "No commentary. No preamble. Your response must start with `{` and end with `}`.\n\n"
        f"{json.dumps(response_model.model_json_schema(), indent=2)}\n"
    )


__all__ = [
    "AnswerQuestionDto",
    "FindingDraftList",
    "StaleCheckDto",
    "VerifyFixDto",
    "assemble_answer_question_prompt",
    "assemble_incremental_review_prompt",
    "assemble_review_prompt",
    "assemble_stale_check_prompt",
    "assemble_verify_fix_prompt",
    "schema_appendix",
]
