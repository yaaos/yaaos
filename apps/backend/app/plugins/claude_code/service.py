"""Claude Code CLI wrapper. Implements `domain/coding_agent.CodingAgentPlugin`.

Vendor-only: this module talks to Anthropic's Claude Code CLI and nothing else.
It spawns ONE parent reviewer per PR review. The parent dispatches yaaos-*
subagents (installed by `installer.py` into `~/.claude/agents/`) via the Task
tool, then synthesizes their findings by re-reading cited code.

Test-mode (stub/replay) wrapping is handled by the `testing/` layer's
`StubCodingAgentPlugin` — see `app.testing.stub_coding_agent`. The bootstrap
in `app/main.py` swaps real plugins for stubs when `YAAOS_CODING_AGENT_STUB`
is set; this file never branches on it.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.primitives import PluginMeta
from app.core.workspace import Workspace, WorkspaceExecError
from app.domain.coding_agent import (
    ActivityEvent,
    HealthStatus,
    IncrementalReviewContext,
    IncrementalReviewResult,
    InvocationStatus,
    InvocationTelemetry,
    OnActivity,
    ReviewContext,
    ReviewResult,
    StaleCheckContext,
    StaleCheckResult,
    ValidationResult,
    VerifyFixContext,
    VerifyFixResult,
    register_coding_agent_plugin,
)
from app.domain.vcs import Finding, FindingSnippetLine
from app.plugins.claude_code.models import ClaudeCodeSettingsRow

log = structlog.get_logger("claude_code")


# Default per-invocation timeout for the Claude Code CLI. Big PRs with parallel
# subagent dispatch + per-finding verification can legitimately take 10-15 min
# on first run; 20 min gives headroom. Per-call override available via
# `ReviewContext.agent_config["timeout_seconds"]`.
_DEFAULT_TIMEOUT_SECONDS = 1200

# Hardcoded model + effort for M01. Future UI work moves these to a settings
# row + per-job override. `--model opus` resolves to the latest Opus alias;
# `--effort medium` is a Claude Code reasoning level (low / medium / high /
# xhigh / max).
_MODEL = "opus"
_EFFORT = "medium"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Plugin-internal response schemas ──────────────────────────────────────────
# These describe the JSON shape we ask Claude Code to emit. They never leak out
# of this plugin — the public Protocol returns `vcs.Finding`/`ReviewResult`.


class _FindingSnippetLine(BaseModel):
    line_number: int
    kind: Literal["context", "add", "del"]
    text: str


class _FindingDto(BaseModel):
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    severity: Literal["must-fix", "nit", "suggestion", "info"]
    title: str
    body: str
    rationale: str | None = None
    snippet: list[_FindingSnippetLine] | None = None
    applied_lesson_ids: list[UUID] = []
    source_agent: str | None = None


class _FindingList(BaseModel):
    findings: list[_FindingDto]


# ── Prompt assembly ───────────────────────────────────────────────────────────

# Parent dispatcher prompt. The reviewer that wraps it is one Claude Code
# subprocess; its job is to (1) decide which yaaos-* subagents apply to this
# PR, (2) dispatch them in parallel via the Task tool, (3) synthesize their
# findings by re-reading any cited code, (4) emit one merged JSON.
#
# Subagent names are listed explicitly so the parent knows what's available
# without us needing to scan the install dir at runtime.
_PARENT_PROMPT_HEADER = """You are the **yaaos parent reviewer**. Your job is to orchestrate a code review of a pull request and produce one synthesized finding list.

You have these subagents available (installed in `~/.claude/agents/`):
- `yaaos-architecture` — module boundaries, patterns, abstractions, CLAUDE.md adherence (always run)
- `yaaos-security` — auth, injection, secrets, crypto misuse (always run)
- `yaaos-line-level` — per-line correctness, idioms, code-level patterns like "no mocks in tests" (always run)
- `yaaos-tests` — test presence and quality for new behavior (always run)
- `yaaos-docs` — documentation sync per CLAUDE.md (always run)
- `yaaos-skill` — Claude Code Skill file validation (run ONLY if the diff touches `**/SKILL.md` or `.claude/skills/**`)

## Your workflow

1. **Read the diff** below to understand what changed.
2. **Decide which subagents to dispatch.** All five always-on subagents plus `yaaos-skill` if and only if the diff touches a skill file. Do not run unnecessary subagents.
3. **DISPATCH ALL RELEVANT SUBAGENTS IN ONE TURN, IN PARALLEL.** In a single assistant response, emit one `Task` tool_use block per relevant subagent. Multiple Task tool_use blocks in the same message run concurrently; sequential Task calls across separate turns run serially and waste minutes. Do not wait for one subagent's result before dispatching the next. Each subagent gets the same brief: the PR title/body and the diff. Each will return a JSON object with `findings`.
4. **Collect their findings.** For each finding, tag it with `source_agent` set to the subagent's name (e.g. `"yaaos-architecture"`).
5. **Synthesize.** Drop duplicates (two subagents finding the same thing — keep the one with better evidence). For each surviving finding, re-read the cited file to confirm the finding is accurate; drop hallucinated findings whose snippet doesn't match what's actually at that location.
6. **Rank by severity** (must-fix > suggestion > nit > info) within each `source_agent` group.
7. **Emit the final JSON.** Schema below. No markdown fences, no preamble.

## Output discipline

- Findings must include `source_agent` so downstream code can attribute each comment.
- Findings must include a verbatim `snippet` (a list of `{line_number, kind, text}` objects from the actual file at HEAD). If you can't produce a verbatim snippet, drop the finding.
- If no findings survive synthesis, emit `{"findings": []}`.
"""


def _assemble_review_prompt(ctx: ReviewContext) -> str:
    parts: list[str] = [_PARENT_PROMPT_HEADER, ""]
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
    if ctx.prior_yaaos_comment_bodies:
        parts.extend(
            [
                "",
                "## Prior yaaos comments on this PR",
                "Don't duplicate them in your final synthesis; build on or disagree.",
                "",
            ]
        )
        for body in ctx.prior_yaaos_comment_bodies[:20]:
            parts.append(f"- {body[:200]}")
    return "\n".join(parts)


def _schema_appendix(response_model: type[BaseModel]) -> str:
    return (
        "\n\n## Output Format (STRICT)\n\n"
        "Respond with EXACTLY a JSON object matching this schema. No markdown fences. "
        "No commentary. No preamble. Your response must start with `{` and end with `}`.\n\n"
        f"{json.dumps(response_model.model_json_schema(), indent=2)}\n"
    )


# ── Stream-json parsing + per-event logging ───────────────────────────────────
#
# Claude Code's `--output-format=stream-json --verbose` emits one JSON object
# per line as work progresses (system init → assistant turns with tool_use
# blocks → user turns with tool_result blocks → terminal `result` event).
# We parse the captured stdout post-hoc (the workspace API doesn't stream
# live yet) and log each event so a timed-out / failed review leaves a
# readable trace in the backend logs.


def _parse_stream_events(stdout: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON. Skip blank / unparseable lines silently —
    Claude Code occasionally interleaves non-JSON noise (e.g., progress dots)
    and we'd rather drop those than abandon the trace."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


def _log_stream_event(event: dict[str, Any]) -> None:
    """Emit one structured log line per stream event.

    Event types: `system` (session init), `assistant` (model output, may
    contain `tool_use` blocks — these are how subagents get dispatched via
    Task), `user` (tool_result blocks — these are subagent return values),
    `result` (terminal envelope with usage + final text).
    """
    et = event.get("type")
    if et == "system":
        log.info(
            "claude_code.stream.system",
            subtype=event.get("subtype"),
            session_id=event.get("session_id"),
            model=event.get("model"),
        )
    elif et == "assistant":
        msg = event.get("message", {}) or {}
        for block in msg.get("content", []) or []:
            btype = block.get("type")
            if btype == "tool_use":
                inp = block.get("input") or {}
                # For Task tool calls, surface which subagent was dispatched.
                subagent = inp.get("subagent_type") if isinstance(inp, dict) else None
                log.info(
                    "claude_code.stream.tool_use",
                    tool=block.get("name"),
                    tool_use_id=block.get("id"),
                    subagent=subagent,
                )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    log.debug("claude_code.stream.assistant_text", excerpt=text[:200])
    elif et == "user":
        msg = event.get("message", {}) or {}
        for block in msg.get("content", []) or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    summary = " ".join(
                        str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in content
                    )
                else:
                    summary = str(content or "")
                log.info(
                    "claude_code.stream.tool_result",
                    tool_use_id=block.get("tool_use_id"),
                    is_error=block.get("is_error", False),
                    excerpt=summary[:200],
                )
    elif et == "result":
        log.info(
            "claude_code.stream.result",
            subtype=event.get("subtype"),
            duration_ms=event.get("duration_ms"),
            num_turns=event.get("num_turns"),
        )


def _render_activity(event: dict[str, Any]) -> ActivityEvent | None:
    """Convert one Claude Code stream event into a user-facing `ActivityEvent`.

    Returns `None` for events with no useful render (e.g. unknown types, empty
    assistant turns). The `message` is pre-rendered for direct UI display; raw
    event data lands in `detail` for the expanded view.
    """
    et = event.get("type")
    ts = _utcnow()
    if et == "system" and event.get("subtype") == "init":
        model = event.get("model") or "?"
        return ActivityEvent(
            ts=ts,
            kind="session_start",
            message=f"Session started · model {model}",
            detail={"model": model, "session_id": event.get("session_id")},
        )
    if et == "assistant":
        msg = event.get("message", {}) or {}
        # An assistant turn may contain a mix of text + tool_use blocks. Render
        # them in order — emit the first block we can, since one ActivityEvent
        # per stream event keeps the feed cardinality 1:1 with stream lines.
        for block in msg.get("content", []) or []:
            btype = block.get("type")
            if btype == "tool_use":
                tool = block.get("name") or "?"
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                if tool == "Task":
                    subagent = inp.get("subagent_type") or "subagent"
                    return ActivityEvent(
                        ts=ts,
                        kind="subagent_dispatched",
                        message=f"Dispatching {subagent}",
                        detail={
                            "subagent": subagent,
                            "tool_use_id": block.get("id"),
                            "description": inp.get("description"),
                        },
                    )
                # Other tool calls — Read, Bash, Grep, Glob, etc.
                target = _summarize_tool_input(tool, inp)
                return ActivityEvent(
                    ts=ts,
                    kind="tool_call_started",
                    message=f"{tool}: {target}" if target else tool,
                    detail={"tool": tool, "tool_use_id": block.get("id"), "input": inp},
                )
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    excerpt = text if len(text) < 200 else text[:197] + "…"
                    return ActivityEvent(
                        ts=ts,
                        kind="assistant_message",
                        message=excerpt,
                        detail={},
                    )
        return None
    if et == "user":
        msg = event.get("message", {}) or {}
        for block in msg.get("content", []) or []:
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, list):
                summary = " ".join(str(c.get("text", c)) if isinstance(c, dict) else str(c) for c in content)
            else:
                summary = str(content or "")
            excerpt = summary.strip()
            if len(excerpt) > 200:
                excerpt = excerpt[:197] + "…"
            return ActivityEvent(
                ts=ts,
                kind="tool_call_finished",
                message=f"→ {excerpt}" if excerpt else "→ (empty result)",
                detail={
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": block.get("is_error", False),
                },
            )
        return None
    if et == "result":
        return ActivityEvent(
            ts=ts,
            kind="result",
            message="Review complete",
            detail={
                "duration_ms": event.get("duration_ms"),
                "num_turns": event.get("num_turns"),
            },
        )
    return None


def _summarize_tool_input(tool: str, inp: dict[str, Any]) -> str:
    """One-line summary of a tool_use's input dict for the activity feed."""
    if tool in ("Read", "Glob", "LS", "NotebookRead"):
        return str(inp.get("file_path") or inp.get("path") or inp.get("pattern") or "")
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        return cmd if len(cmd) < 120 else cmd[:117] + "…"
    if tool == "Grep":
        pat = str(inp.get("pattern") or "")
        path = str(inp.get("path") or "")
        return f"{pat!r} in {path}" if path else f"{pat!r}"
    if tool == "WebFetch":
        return str(inp.get("url") or "")
    if tool == "WebSearch":
        return str(inp.get("query") or "")
    if tool == "TodoWrite":
        todos = inp.get("todos") or []
        return f"{len(todos)} todos" if isinstance(todos, list) else ""
    return ""


# ── Verdict ───────────────────────────────────────────────────────────────────


def _compute_state(findings: list[_FindingDto]) -> Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"]:
    if not findings:
        return "APPROVED"
    if any(f.severity == "must-fix" for f in findings):
        return "CHANGES_REQUESTED"
    return "COMMENT"


def _dto_to_finding(dto: _FindingDto) -> Finding:
    snippet: list[FindingSnippetLine] | None = None
    if dto.snippet:
        snippet = [
            FindingSnippetLine(line_number=s.line_number, kind=s.kind, text=s.text) for s in dto.snippet
        ]
    return Finding(
        file=dto.file,
        line_start=dto.line_start,
        line_end=dto.line_end,
        severity=dto.severity,
        title=dto.title,
        body=dto.body,
        rationale=dto.rationale,
        snippet=snippet,
        applied_lesson_ids=dto.applied_lesson_ids,
        source_agent=dto.source_agent,
    )


# ── Plugin ────────────────────────────────────────────────────────────────────


class ClaudeCodePlugin:
    meta = PluginMeta(
        id="claude_code",
        type="coding_agent",
        display_name="Claude Code",
        description="Wraps Anthropic's Claude Code CLI to run code reviews and replies.",
        docs_url="https://docs.claude.com/en/docs/claude-code",
    )

    async def _load_settings_for_invocation(self) -> tuple[str | None, str | None]:
        """Returns (decrypted_api_key, cli_path). Timeout is a constant — see
        `_DEFAULT_TIMEOUT_SECONDS`. Per-call override via `agent_config["timeout_seconds"]`."""
        async with db_session() as s:
            row = (await s.execute(select(ClaudeCodeSettingsRow).limit(1))).scalar_one_or_none()
        if row is None:
            return None, None
        api_key: str | None = None
        if row.encrypted_anthropic_api_key:
            try:
                fernet = Fernet(get_settings().yaaos_encryption_key.encode())
                api_key = fernet.decrypt(row.encrypted_anthropic_api_key).decode()
            except InvalidToken:
                log.warning("claude_code.api_key_decrypt_failed")
        return api_key, row.cli_path

    async def review(
        self,
        workspace: Workspace,
        context: ReviewContext,
        on_activity: OnActivity | None = None,
    ) -> ReviewResult:
        prep = await self._prepare_invocation(context.agent_config)
        if isinstance(prep, ReviewResult):
            return prep
        argv, env, timeout = prep

        full_prompt = _assemble_review_prompt(context) + _schema_appendix(_FindingList)

        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout, on_activity)
        if isinstance(envelope, ReviewResult):
            return envelope
        agent_text, telemetry = envelope

        try:
            parsed_dict = json.loads(agent_text)
            parsed = _FindingList.model_validate(parsed_dict)
        except (json.JSONDecodeError, ValidationError) as e:
            return ReviewResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _FindingList: {e}",
            )

        return ReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=[_dto_to_finding(f) for f in parsed.findings],
            state=_compute_state(parsed.findings),
            summary_body=None,
            lesson_ids_consulted=[lesson.id for lesson in context.lessons],
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

    async def _prepare_invocation(
        self, agent_config: dict[str, Any]
    ) -> tuple[list[str], dict[str, str], int] | ReviewResult:
        """Load settings, build argv + env. Returns ReviewResult on early failure.

        (Reply path coerces the result; same error shape applies.)
        """
        api_key, cli_path_setting = await self._load_settings_for_invocation()
        if not api_key:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                error_message="ANTHROPIC_API_KEY not set in claude_code_settings",
            )
        cli_path = cli_path_setting or shutil.which("claude")
        if not cli_path:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                error_message="claude binary not found on PATH or in claude_code_settings.cli_path",
            )

        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = api_key
        timeout = agent_config.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS
        argv = [
            cli_path,
            "--print",
            # stream-json emits one JSON event per line as work progresses.
            # The workspace's streaming path forwards each line to a callback
            # so we can publish ReviewJobActivity SSE events live + persist
            # the full sequence on completion. `--verbose` is required when
            # streaming JSON.
            "--output-format=stream-json",
            "--verbose",
            "--permission-mode=bypassPermissions",
            # Model + effort hardcoded for M01 (future UI configures them).
            "--model",
            _MODEL,
            "--effort",
            _EFFORT,
            # Task is required so the parent reviewer can dispatch yaaos-* subagents.
            # Bash is restricted to read-only git commands so subagents can run
            # `git diff <base_sha>..HEAD` themselves instead of yaaos inlining
            # the entire diff into the prompt (saves tens of thousands of
            # tokens on big PRs and avoids duplicating the diff across N
            # subagent task briefs).
            "--allowed-tools=Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch,Task,"
            "Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),"
            "Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status)",
        ]
        return argv, env, timeout

    async def _run_and_parse_envelope(
        self,
        workspace: Workspace,
        argv: list[str],
        env: dict[str, str],
        full_prompt: str,
        timeout: int,
        on_activity: OnActivity | None,
    ) -> tuple[str, InvocationTelemetry] | ReviewResult:
        """Run the CLI via the workspace; parse the wrapper envelope.

        Streams stdout line-by-line: each line is parsed into a stream event,
        logged, rendered to an `ActivityEvent`, and forwarded via `on_activity`
        (if supplied). After the subprocess exits, the final `result` event
        yields the agent text + tokens + resolved model.

        Returns (agent_text, telemetry) on success, or a `ReviewResult` carrying
        the failure status.
        """
        events: list[dict[str, Any]] = []

        async def _on_stream_line(line: bytes) -> None:
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                return
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                return  # malformed line — skip; final agent_text comes from `result`
            events.append(event)
            _log_stream_event(event)
            if on_activity is None:
                return
            activity = _render_activity(event)
            if activity is None:
                return
            try:
                await on_activity(activity)
            except Exception:
                log.exception("claude_code.on_activity_failed", kind=activity.kind)

        try:
            result = await workspace.run_coding_agent_cli(
                argv=argv,
                env=env,
                stdin=full_prompt.encode("utf-8"),
                timeout_seconds=timeout,
                on_stream_line=_on_stream_line,
            )
        except WorkspaceExecError as e:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                error_message=f"could not spawn claude: {e}",
            )

        telemetry = InvocationTelemetry(latency_ms=result.duration_ms, raw_stderr=result.stderr)
        final_result_event = next((e for e in reversed(events) if e.get("type") == "result"), None)

        if result.timed_out:
            return ReviewResult(
                status=InvocationStatus.TIMEOUT,
                telemetry=telemetry.model_copy(update={"raw_output": result.stdout}),
                error_message=(
                    f"claude did not return within {timeout}s (parsed {len(events)} stream events)"
                ),
            )

        if result.exit_code != 0:
            first_line = result.stderr.splitlines()[0] if result.stderr else ""
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                telemetry=telemetry.model_copy(update={"raw_output": result.stdout}),
                error_message=f"claude exited {result.exit_code}: {first_line}",
            )

        if final_result_event is None:
            return ReviewResult(
                status=InvocationStatus.AGENT_ERROR,
                telemetry=telemetry.model_copy(update={"raw_output": result.stdout}),
                error_message="claude stream contained no `result` event",
            )

        agent_text = final_result_event.get("result", "")
        usage = final_result_event.get("usage", {}) or {}
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        # The CLI reports the resolved model in the final result event so an
        # alias like `opus` becomes a versioned name on the row.
        resolved_model = final_result_event.get("model") or _MODEL

        telemetry = telemetry.model_copy(
            update={
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model": resolved_model,
            }
        )
        return agent_text, telemetry

    async def incremental_review(
        self,
        workspace: Workspace,
        context: IncrementalReviewContext,
        on_activity: OnActivity | None = None,
    ) -> IncrementalReviewResult:
        # Wired when domain/reviewer ships the incremental-review flow. The
        # prompt template + structured-output schema for this mode are owned
        # by domain/reviewer per plan §5.5.
        raise NotImplementedError("claude_code: incremental_review not wired yet")

    async def verify_fix(
        self,
        workspace: Workspace,
        context: VerifyFixContext,
        on_activity: OnActivity | None = None,
    ) -> VerifyFixResult:
        raise NotImplementedError("claude_code: verify_fix not wired yet")

    async def stale_check(
        self,
        workspace: Workspace,
        context: StaleCheckContext,
        on_activity: OnActivity | None = None,
    ) -> StaleCheckResult:
        raise NotImplementedError("claude_code: stale_check not wired yet")

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        if "timeout_seconds" in agent_config:
            v = agent_config["timeout_seconds"]
            if not isinstance(v, int) or v <= 0:
                errors.append("timeout_seconds must be a positive int")
        unknown = set(agent_config) - {"timeout_seconds"}
        errors.extend(f"unknown config key: {k}" for k in unknown)
        return ValidationResult(valid=not errors, errors=errors)

    async def health_check(self) -> HealthStatus:
        api_key, cli_path_setting = await self._load_settings_for_invocation()
        if not api_key:
            return HealthStatus(healthy=False, message="anthropic api key not set", checked_at=_utcnow())
        cli_path = cli_path_setting or shutil.which("claude")
        if not cli_path:
            return HealthStatus(healthy=False, message="claude binary not found", checked_at=_utcnow())
        # Cached probe — verifies the key actually authenticates against Anthropic.
        # Cache TTL keeps the cost low (~1 request per 5min per running process).
        ok, message = await _probe_anthropic_auth(api_key)
        return HealthStatus(healthy=ok, message=message, checked_at=_utcnow())


_plugin = ClaudeCodePlugin()


# ── Anthropic auth probe ──────────────────────────────────────────────────────
# Keyed by sha256(key) so cache survives across re-reads of the same value and
# automatically resets when the key changes. `_set_anthropic_key` also flushes
# explicitly so a rotation never serves a stale cached result.

_AUTH_TTL = timedelta(minutes=5)
_AUTH_CACHE: dict[str, tuple[bool, str, datetime]] = {}


def _key_fingerprint(key: str) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(key.encode()).hexdigest()


async def _probe_anthropic_auth(api_key: str) -> tuple[bool, str]:
    """Return (healthy, message). Cached for `_AUTH_TTL` per key fingerprint.

    In stub mode (`YAAOS_CODING_AGENT_STUB`), the e2e test stack has no
    outbound connectivity to `api.anthropic.com` — and shouldn't need it,
    since the stub agent never calls Anthropic anyway. Treat any non-empty
    key as authenticating cleanly so onboarding and `/api/claude_code/health`
    behave consistently with the rest of the stubbed pipeline.
    """
    if os.environ.get("YAAOS_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
        return (True, "ok (stub)")
    fp = _key_fingerprint(api_key)
    now = _utcnow()
    cached = _AUTH_CACHE.get(fp)
    if cached and cached[2] > now:
        return cached[0], cached[1]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        if r.status_code == 200:
            result = (True, "ok")
        elif r.status_code in (401, 403):
            result = (False, "anthropic api key is invalid")
        else:
            result = (False, f"anthropic api returned HTTP {r.status_code}")
    except (httpx.HTTPError, OSError) as e:
        result = (False, f"could not reach anthropic api: {e}")
    _AUTH_CACHE[fp] = (result[0], result[1], now + _AUTH_TTL)
    return result


def _invalidate_auth_cache() -> None:
    _AUTH_CACHE.clear()


async def _onboarding_anthropic_key_set(org_id: UUID) -> bool:
    """Settings contributor — returns True iff a working key is present.

    "Set" means: there's an encrypted row in the DB AND the key actually
    authenticates against Anthropic. A saved-but-invalid key (e.g., a typo or a
    rotated/revoked credential) does not satisfy the prereq — the onboarding
    stepper would otherwise stay green when reviews would fail.

    The auth probe is cached by `_probe_anthropic_auth` (5-minute TTL per key
    fingerprint), so a 5-second polling dashboard makes at most one HTTP call
    per 5 minutes per key.
    """
    async with db_session() as s:
        row = (
            await s.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org_id))
        ).scalar_one_or_none()
    if row is None or row.encrypted_anthropic_api_key is None:
        return False
    try:
        fernet = Fernet(get_settings().yaaos_encryption_key.encode())
        api_key = fernet.decrypt(row.encrypted_anthropic_api_key).decode()
    except InvalidToken:
        return False
    healthy, _ = await _probe_anthropic_auth(api_key)
    return healthy


async def _set_anthropic_key(org_id: UUID, raw_key: str) -> None:
    """Encrypt + upsert the Anthropic key on `claude_code_settings`."""
    from uuid import uuid4  # noqa: PLC0415

    fernet = Fernet(get_settings().yaaos_encryption_key.encode())
    enc = fernet.encrypt(raw_key.encode())
    async with db_session() as s:
        row = (
            await s.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org_id))
        ).scalar_one_or_none()
        if row is None:
            row = ClaudeCodeSettingsRow(
                id=uuid4(),
                org_id=org_id,
                encrypted_anthropic_api_key=enc,
            )
            s.add(row)
        else:
            row.encrypted_anthropic_api_key = enc
        await s.commit()
    # Rotation should never serve a stale "healthy" verdict from the previous key.
    _invalidate_auth_cache()


def bootstrap() -> None:
    from app.domain.settings import register_onboarding_contributor  # noqa: PLC0415
    from app.plugins.claude_code.installer import install_subagents  # noqa: PLC0415

    register_coding_agent_plugin(_plugin)
    register_onboarding_contributor("anthropic_key_set", _onboarding_anthropic_key_set)
    # Install yaaos-* subagent definitions so the parent reviewer can dispatch
    # them via the Task tool. Static files, idempotent — fine to run on every
    # backend startup. M02+ Docker-workspace isolation will move this per-
    # workspace; today there's one HOME shared by all reviews.
    try:
        install_subagents()
    except OSError as e:
        log.warning("claude_code.subagent_install_failed", error=str(e))


def get_plugin() -> ClaudeCodePlugin:
    return _plugin


def get_set_anthropic_key():
    """Public accessor for the credential setter — used by the plugin's web routes."""
    return _set_anthropic_key
