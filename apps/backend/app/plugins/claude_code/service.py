"""Claude Code CLI wrapper. Implements `domain/coding_agent.CodingAgentPlugin`.

Vendor-only: this module talks to Anthropic's Claude Code CLI and nothing else.
It spawns ONE parent reviewer per PR review. The parent dispatches yaaos-*
subagents (installed by `installer.py` into `~/.claude/agents/`) via the Task
tool, then synthesizes their findings by re-reading cited code.

Test-mode (stub/replay) wrapping is handled by the `testing/` layer's
`StubCodingAgentPlugin` — see `app.testing.stub_coding_agent`. The bootstrap
in `app/web.py` swaps real plugins for stubs when `YAAOS_CODING_AGENT_STUB`
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
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.plugin_kit import PluginMeta
from app.core.workspace import Workspace, WorkspaceExecError
from app.domain.coding_agent import (
    ActivityEvent,
    AnswerQuestionContext,
    AnswerQuestionResult,
    FindingAnchor,
    FindingDraft,
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
    register_plugin,
)
from app.domain.coding_agent import (
    AnswerQuestionDto as _AnswerQuestionDto,
)
from app.domain.coding_agent import (
    FindingDraftList as _FindingDraftList,
)
from app.domain.coding_agent import (
    StaleCheckDto as _StaleCheckDto,
)
from app.domain.coding_agent import (
    VerifyFixDto as _VerifyFixDto,
)
from app.domain.coding_agent import (
    assemble_answer_question_prompt as _assemble_answer_question_prompt,
)
from app.domain.coding_agent import (
    assemble_incremental_review_prompt as _assemble_incremental_review_prompt,
)
from app.domain.coding_agent import (
    assemble_review_prompt as _assemble_review_prompt,
)
from app.domain.coding_agent import (
    assemble_stale_check_prompt as _assemble_stale_check_prompt,
)
from app.domain.coding_agent import (
    assemble_verify_fix_prompt as _assemble_verify_fix_prompt,
)
from app.domain.coding_agent import (
    schema_appendix as _schema_appendix,
)
from app.plugins.claude_code.models import ClaudeCodeSettingsRow

log = structlog.get_logger("claude_code")


# Default per-invocation timeout for the Claude Code CLI. Big PRs with parallel
# subagent dispatch + per-finding verification can legitimately take 10-15 min
# on first run; 20 min gives headroom. Per-call override available via
# `ReviewContext.agent_config["timeout_seconds"]`.
_DEFAULT_TIMEOUT_SECONDS = 1200

# Hardcoded model + effort for . Future UI work moves these to a settings
# row + per-job override. `--model opus` resolves to the latest Opus alias;
# `--effort medium` is a Claude Code reasoning level (low / medium / high /
# xhigh / max).
_MODEL = "opus"
_EFFORT = "medium"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _pick_versioned_model(*candidates: str | None) -> str | None:
    """Return the first candidate that looks like a versioned model name.

    The Claude Code CLI sometimes announces the resolved versioned name
    (`claude-opus-4-5-20250929`) in the `system.init` event but echoes only
    the alias (`opus`) in the final `result` event. A versioned name has
    hyphens; an alias does not. Returns the first hyphen-bearing candidate;
    falls back to the first non-empty candidate; None if all are empty.
    """
    non_empty = [c for c in candidates if c]
    if not non_empty:
        return None
    versioned = next((c for c in non_empty if "-" in c), None)
    return versioned or non_empty[0]


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
                    # Trust-boundary: ActivityEvents cross from the customer's
                    # workspace to yaaos' control plane. `inp` for Edit / Write
                    # tools carries the full source content the agent is about
                    # to commit; we MUST NOT leak it across the boundary. Only
                    # metadata fields (paths, command summaries) are kept.
                    detail={
                        "tool": tool,
                        "tool_use_id": block.get("id"),
                        "input_summary": _safe_tool_input(tool, inp),
                    },
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
            # Trust-boundary: a tool_result is the raw output of running a
            # tool inside the customer's workspace (file body for Read,
            # stdout for Bash, match lines for Grep). It must NOT cross
            # into the activity feed — only the size + error flag may.
            content = block.get("content")
            if isinstance(content, list):
                size_bytes = sum(
                    len(str(c.get("text", c))) if isinstance(c, dict) else len(str(c)) for c in content
                )
            else:
                size_bytes = len(str(content or ""))
            is_error = bool(block.get("is_error", False))
            if is_error:
                message = "→ error"
            else:
                message = f"→ ok ({size_bytes} bytes)" if size_bytes else "→ ok (empty)"
            return ActivityEvent(
                ts=ts,
                kind="tool_call_finished",
                message=message,
                detail={
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": is_error,
                    "size_bytes": size_bytes,
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


# Tool names whose `input` dicts may carry full source content (file bodies,
# diff hunks) that must NOT cross the activity-stream trust boundary into the
# yaaos control plane. The pre-renderer keeps only metadata fields for these.
_CONTENT_BEARING_TOOLS: frozenset[str] = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


def _safe_tool_input(tool: str, inp: dict[str, Any]) -> dict[str, Any]:
    """Metadata-only projection of a tool_use's input dict, safe to ship in
    an `ActivityEvent.detail` across the workspace → control-plane trust
    boundary.

    - Edit/Write tools: keep only `file_path` (target identity); the body
      / new_string / content fields stay inside the workspace.
    - Read/Glob/LS/NotebookRead: keep `file_path` or `path` (target only).
    - Bash: keep a 120-char prefix of the command (caller already summarized
      it for `message`; `detail` mirrors that prefix without expansion).
    - Grep: keep `pattern` + `path`.
    - WebFetch/WebSearch: keep `url` / `query`.
    - TodoWrite: keep `len(todos)` only.
    - Unknown tools: return empty dict — better to drop than leak.
    """
    if tool in _CONTENT_BEARING_TOOLS:
        path = inp.get("file_path") or inp.get("path")
        return {"file_path": str(path)} if path else {}
    if tool in ("Read", "Glob", "LS", "NotebookRead"):
        path = inp.get("file_path") or inp.get("path") or inp.get("pattern")
        return {"target": str(path)} if path else {}
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        return {"command_prefix": cmd if len(cmd) < 120 else cmd[:117] + "…"}
    if tool == "Grep":
        return {
            "pattern": str(inp.get("pattern") or ""),
            "path": str(inp.get("path") or ""),
        }
    if tool == "WebFetch":
        return {"url": str(inp.get("url") or "")}
    if tool == "WebSearch":
        return {"query": str(inp.get("query") or "")}
    if tool == "TodoWrite":
        todos = inp.get("todos") or []
        return {"todo_count": len(todos) if isinstance(todos, list) else 0}
    return {}


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


def _compute_state_v2(findings: list[FindingDraft]) -> Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"]:
    """Plan §10.1 severity tiers — `blocker`/`major` request changes."""
    if not findings:
        return "APPROVED"
    if any(f.severity in {"blocker", "major"} for f in findings):
        return "CHANGES_REQUESTED"
    return "COMMENT"


# ── Plugin ────────────────────────────────────────────────────────────────────


class ClaudeCodePlugin:
    meta = PluginMeta(
        id="claude_code",
        type="coding_agent",
        display_name="Claude Code",
        description="Wraps Anthropic's Claude Code CLI to run code reviews and replies.",
        docs_url="https://docs.claude.com/en/docs/claude-code",
    )

    def install_url(self, org_id: UUID) -> str | None:
        """No out-of-band install — Claude Code settings are pure form. The
        bespoke settings page (Phase 10) handles it."""
        del org_id
        return None

    def validate_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Full Pydantic validation: orchestrator + agents shape, enums on
        model/version/effort, agent count 1..8, name uniqueness within
        agents. See `settings_schema.validate_settings`. Accepts an empty
        dict and substitutes defaults so the picker's `POST /api/coding-
        agents` install path doesn't have to pre-populate settings."""
        from app.plugins.claude_code.defaults import get_defaults  # noqa: PLC0415
        from app.plugins.claude_code.settings_schema import (  # noqa: PLC0415
            validate_settings as _validate,
        )

        if not settings:
            d = get_defaults()
            settings = {"orchestrator": d["orchestrator"], "agents": d["agents"]}
        return _validate(settings)

    async def _load_settings_for_invocation(self) -> tuple[SecretStr | None, str | None]:
        """Returns (decrypted_api_key, cli_path). Timeout is a constant — see
        `_DEFAULT_TIMEOUT_SECONDS`. Per-call override via `agent_config["timeout_seconds"]`."""
        async with db_session() as s:
            row = (await s.execute(select(ClaudeCodeSettingsRow).limit(1))).scalar_one_or_none()
        if row is None:
            return None, None
        api_key: SecretStr | None = None
        if row.encrypted_anthropic_api_key:
            try:
                fernet = Fernet(get_settings().yaaos_encryption_key.get_secret_value().encode())
                api_key = SecretStr(fernet.decrypt(row.encrypted_anthropic_api_key).decode())
            except InvalidToken:
                log.warning("claude_code.api_key_decrypt_failed")
        return api_key, row.cli_path

    async def review(
        self,
        workspace: Workspace,
        context: ReviewContext,
        on_activity: OnActivity | None = None,
    ) -> ReviewResult:
        mcp_tools = await _materialize_mcp_config(workspace, context.agent_config.get("mcp"))
        prep = await self._prepare_invocation(context.agent_config, extra_allowed_tools=mcp_tools)
        if isinstance(prep, ReviewResult):
            return prep
        argv, env, timeout = prep

        # Plan §6.1 + §13 cutover: full review emits the §10.1 schema
        # (FindingDraft) — same shape as incremental review. The reviewer
        # module (queue.py) handles admission + posting in one place.
        full_prompt = _assemble_review_prompt(context) + _schema_appendix(_FindingDraftList)

        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout, on_activity)
        if isinstance(envelope, ReviewResult):
            return envelope
        agent_text, telemetry = envelope

        try:
            parsed_dict = json.loads(agent_text)
            parsed = _FindingDraftList.model_validate(parsed_dict)
        except (json.JSONDecodeError, ValidationError) as e:
            return ReviewResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _FindingDraftList: {e}",
            )

        drafts = [
            FindingDraft(
                severity=d.severity,
                rule_id=d.rule_id,
                title=d.title,
                body=d.body,
                concrete_failure_scenario=d.concrete_failure_scenario,
                confidence=d.confidence,
                rationale=d.rationale,
                anchor=FindingAnchor(file_path=d.file_path, line_start=d.line_start, line_end=d.line_end),
                duplicate_of_rule_ids=d.duplicate_of_rule_ids,
            )
            for d in parsed.findings
        ]
        return ReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=drafts,
            state=_compute_state_v2(drafts),
            summary_body=None,
            lesson_ids_consulted=[lesson.id for lesson in context.lessons],
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

    async def _prepare_invocation(
        self,
        agent_config: dict[str, Any],
        *,
        allowed_tools_override: str | None = None,
        extra_allowed_tools: list[str] | None = None,
    ) -> tuple[list[str], dict[str, str], int] | ReviewResult:
        """Load settings, build argv + env. Returns ReviewResult on early failure.

        `allowed_tools_override` swaps the `--allowed-tools` value used by the
        full review (which includes `Task` for subagent dispatch) for a leaner
        set — `answer_question` uses a read-only repo + git list with no Task.
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
        env["ANTHROPIC_API_KEY"] = api_key.get_secret_value()
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
            # Model + effort hardcoded (future UI configures them).
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
            "--allowed-tools="
            + (
                allowed_tools_override
                or (
                    "Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch,Task,"
                    "Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),"
                    "Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status)"
                )
            )
            + ("," + ",".join(extra_allowed_tools) if extra_allowed_tools else ""),
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
        # The CLI announces the resolved versioned model name (e.g.
        # `claude-opus-4-5-20250929`) in the `system.init` event. The `result`
        # event sometimes echoes only the alias the caller passed in (`opus`).
        # Prefer init when it carries a versioned name (anything that looks
        # like an alias-only — no hyphens — falls through to the result-event
        # value, then to the static `_MODEL` constant).
        init_event = next(
            (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None
        )
        init_model = (init_event or {}).get("model")
        result_model = final_result_event.get("model")
        resolved_model = _pick_versioned_model(init_model, result_model) or _MODEL

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
        prep = await self._prepare_invocation(context.agent_config)
        if isinstance(prep, ReviewResult):
            return IncrementalReviewResult(
                status=prep.status,
                error_message=prep.error_message,
                telemetry=prep.telemetry,
            )
        argv, env, timeout = prep

        full_prompt = _assemble_incremental_review_prompt(context) + _schema_appendix(_FindingDraftList)
        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout, on_activity)
        if isinstance(envelope, ReviewResult):
            return IncrementalReviewResult(
                status=envelope.status,
                error_message=envelope.error_message,
                telemetry=envelope.telemetry,
            )
        agent_text, telemetry = envelope

        try:
            parsed_dict = json.loads(agent_text)
            parsed = _FindingDraftList.model_validate(parsed_dict)
        except (json.JSONDecodeError, ValidationError) as e:
            return IncrementalReviewResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _FindingDraftList: {e}",
            )

        drafts = [
            FindingDraft(
                severity=d.severity,
                rule_id=d.rule_id,
                title=d.title,
                body=d.body,
                concrete_failure_scenario=d.concrete_failure_scenario,
                confidence=d.confidence,
                rationale=d.rationale,
                anchor=FindingAnchor(file_path=d.file_path, line_start=d.line_start, line_end=d.line_end),
                duplicate_of_rule_ids=d.duplicate_of_rule_ids,
            )
            for d in parsed.findings
        ]
        return IncrementalReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=drafts,
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

    async def verify_fix(
        self,
        workspace: Workspace,
        context: VerifyFixContext,
        on_activity: OnActivity | None = None,
    ) -> VerifyFixResult:
        prep = await self._prepare_invocation(context.agent_config)
        if isinstance(prep, ReviewResult):
            return VerifyFixResult(
                status=prep.status, error_message=prep.error_message, telemetry=prep.telemetry
            )
        argv, env, timeout = prep
        full_prompt = _assemble_verify_fix_prompt(context) + _schema_appendix(_VerifyFixDto)
        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout, on_activity)
        if isinstance(envelope, ReviewResult):
            return VerifyFixResult(
                status=envelope.status, error_message=envelope.error_message, telemetry=envelope.telemetry
            )
        agent_text, telemetry = envelope
        try:
            parsed = _VerifyFixDto.model_validate(json.loads(agent_text))
        except (json.JSONDecodeError, ValidationError) as e:
            return VerifyFixResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _VerifyFixDto: {e}",
            )
        return VerifyFixResult(
            status=InvocationStatus.SUCCESS,
            still_present=parsed.still_present,
            confidence=parsed.confidence,
            reasoning=parsed.reasoning,
            observed_line=parsed.observed_line,
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

    async def stale_check(
        self,
        workspace: Workspace,
        context: StaleCheckContext,
        on_activity: OnActivity | None = None,
    ) -> StaleCheckResult:
        prep = await self._prepare_invocation(context.agent_config)
        if isinstance(prep, ReviewResult):
            return StaleCheckResult(
                status=prep.status, error_message=prep.error_message, telemetry=prep.telemetry
            )
        argv, env, timeout = prep
        full_prompt = _assemble_stale_check_prompt(context) + _schema_appendix(_StaleCheckDto)
        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout, on_activity)
        if isinstance(envelope, ReviewResult):
            return StaleCheckResult(
                status=envelope.status, error_message=envelope.error_message, telemetry=envelope.telemetry
            )
        agent_text, telemetry = envelope
        try:
            parsed = _StaleCheckDto.model_validate(json.loads(agent_text))
        except (json.JSONDecodeError, ValidationError) as e:
            return StaleCheckResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _StaleCheckDto: {e}",
            )
        return StaleCheckResult(
            status=InvocationStatus.SUCCESS,
            still_applies=parsed.still_applies,
            confidence=parsed.confidence,
            reasoning=parsed.reasoning,
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

    async def answer_question(
        self,
        workspace: Workspace,
        context: AnswerQuestionContext,
        on_activity: OnActivity | None = None,
    ) -> AnswerQuestionResult:
        # Read-only repo + git tools only — no `Task` subagent dispatch (the
        # question runner answers itself) and no `Write`/`Edit`/`Bash` beyond
        # whitelisted git commands. Same authentication path as `verify_fix`.
        prep = await self._prepare_invocation(
            context.agent_config,
            allowed_tools_override=(
                "Read,Glob,Grep,LS,"
                "Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),"
                "Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status)"
            ),
        )
        if isinstance(prep, ReviewResult):
            return AnswerQuestionResult(
                status=prep.status, error_message=prep.error_message, telemetry=prep.telemetry
            )
        argv, env, timeout = prep
        full_prompt = _assemble_answer_question_prompt(context) + _schema_appendix(_AnswerQuestionDto)
        envelope = await self._run_and_parse_envelope(workspace, argv, env, full_prompt, timeout, on_activity)
        if isinstance(envelope, ReviewResult):
            return AnswerQuestionResult(
                status=envelope.status, error_message=envelope.error_message, telemetry=envelope.telemetry
            )
        agent_text, telemetry = envelope
        try:
            parsed = _AnswerQuestionDto.model_validate(json.loads(agent_text))
        except (json.JSONDecodeError, ValidationError) as e:
            return AnswerQuestionResult(
                status=InvocationStatus.PARSE_FAILURE,
                telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
                error_message=f"agent response didn't match _AnswerQuestionDto: {e}",
            )
        return AnswerQuestionResult(
            status=InvocationStatus.SUCCESS,
            answer=parsed.answer,
            telemetry=telemetry.model_copy(update={"raw_output": agent_text}),
        )

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


_MCP_CONFIG_FILE = ".mcp.json"


async def _materialize_mcp_config(
    workspace: Workspace,
    mcp_payload: dict[str, Any] | None,
) -> list[str]:
    """Write a Claude-Code `.mcp.json` into the workspace from the reviewer's
    MCP payload. Returns the per-server `mcp__<server>__<tool>` allowed-tools
    additions (defense in depth — the proxy is the actual gate).

    Returns an empty list when no payload is provided.
    """
    if not mcp_payload or not mcp_payload.get("servers"):
        return []
    token = mcp_payload["token"]
    base_url = mcp_payload["base_url"]
    config = {
        "mcpServers": {
            s["provider"]: {
                "type": "http",
                "url": f"{base_url}/{s['provider']}",
                "headers": {"Authorization": f"Bearer {token}"},
            }
            for s in mcp_payload["servers"]
        }
    }
    await workspace.write_text(_MCP_CONFIG_FILE, json.dumps(config, indent=2))
    extras: list[str] = []
    for s in mcp_payload["servers"]:
        provider = s["provider"]
        allowed = set(s.get("allowed_tools") or [])
        for tool in s.get("known_read_tools", []):
            extras.append(f"mcp__{provider}__{tool}")
        for tool in s.get("known_write_tools", []):
            if tool in allowed:
                extras.append(f"mcp__{provider}__{tool}")
    return extras


# ── Anthropic auth probe ──────────────────────────────────────────────────────
# Keyed by sha256(key) so cache survives across re-reads of the same value and
# automatically resets when the key changes. `_set_anthropic_key` also flushes
# explicitly so a rotation never serves a stale cached result.

_AUTH_TTL = timedelta(minutes=5)
_AUTH_CACHE: dict[str, tuple[bool, str, datetime]] = {}


def _key_fingerprint(key: str) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(key.encode()).hexdigest()


async def _probe_anthropic_auth(api_key: SecretStr) -> tuple[bool, str]:
    """Return (healthy, message). Cached for `_AUTH_TTL` per key fingerprint.

    In stub mode (`YAAOS_CODING_AGENT_STUB`), the e2e test stack has no
    outbound connectivity to `api.anthropic.com` — and shouldn't need it,
    since the stub agent never calls Anthropic anyway. Treat any non-empty
    key as authenticating cleanly so onboarding and `/api/claude_code/health`
    behave consistently with the rest of the stubbed pipeline.
    """
    if os.environ.get("YAAOS_CODING_AGENT_STUB", "").lower() in {"1", "true", "yes"}:
        return (True, "ok (stub)")
    raw_key = api_key.get_secret_value()
    fp = _key_fingerprint(raw_key)
    now = _utcnow()
    cached = _AUTH_CACHE.get(fp)
    if cached and cached[2] > now:
        return cached[0], cached[1]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": raw_key, "anthropic-version": "2023-06-01"},
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
        fernet = Fernet(get_settings().yaaos_encryption_key.get_secret_value().encode())
        api_key = SecretStr(fernet.decrypt(row.encrypted_anthropic_api_key).decode())
    except InvalidToken:
        return False
    healthy, _ = await _probe_anthropic_auth(api_key)
    return healthy


async def set_api_key(session, *, org_id: UUID, encrypted_anthropic_api_key: bytes) -> None:
    """Write or update the ``claude_code_settings`` row for *org_id*.

    Accepts a pre-encrypted value (callers use ``cryptography.fernet.Fernet``
    to encrypt). A second call updates rather than duplicates — upserts on
    ``org_id`` which is UNIQUE.

    Shape (a) — takes ``session`` first positional; never commits. Caller
    composes with sibling writes inside one ``async with db_session()`` block.
    See ``apps/backend/docs/patterns.md`` § Service-fn session-handling convention.
    """
    from uuid import uuid4 as _uuid4  # noqa: PLC0415

    row = (
        await session.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        row = ClaudeCodeSettingsRow(
            id=_uuid4(),
            org_id=org_id,
            encrypted_anthropic_api_key=encrypted_anthropic_api_key,
        )
        session.add(row)
    else:
        row.encrypted_anthropic_api_key = encrypted_anthropic_api_key
    await session.flush()


async def _set_anthropic_key(org_id: UUID, raw_key: SecretStr) -> None:
    """Encrypt + upsert the Anthropic key on `claude_code_settings`."""
    from uuid import uuid4  # noqa: PLC0415

    plaintext = raw_key.get_secret_value()
    fernet = Fernet(get_settings().yaaos_encryption_key.get_secret_value().encode())
    enc = fernet.encrypt(plaintext.encode())
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
    # Make the key visible to `core/llm` (LangChain `init_chat_model` resolves
    # auth via `ANTHROPIC_API_KEY`) immediately — fresh onboarding shouldn't
    # require a backend restart before the classifier can authenticate.
    os.environ["ANTHROPIC_API_KEY"] = plaintext


async def bootstrap_anthropic_env() -> None:
    """Populate `ANTHROPIC_API_KEY` from the encrypted DB row at app startup.

    `core/llm` (LangChain `init_chat_model`) authenticates from the process env;
    yaaos stores the key encrypted in `claude_code_settings`. Without this hook,
    a freshly-booted container can't make direct LLM calls (e.g., classifier)
    until the next time `_set_anthropic_key` runs. Pre-onboarding (no row yet)
    is a normal state — the hook silently no-ops and the classifier surfaces
    its own "key not set" error if it ever runs.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return  # Already populated (e.g., Braintrust gateway, test env).
    async with db_session() as s:
        row = (await s.execute(select(ClaudeCodeSettingsRow).limit(1))).scalar_one_or_none()
    if row is None or row.encrypted_anthropic_api_key is None:
        log.info("claude_code.bootstrap_env.no_key", reason="pre_onboarding")
        return
    try:
        fernet = Fernet(get_settings().yaaos_encryption_key.get_secret_value().encode())
        os.environ["ANTHROPIC_API_KEY"] = fernet.decrypt(row.encrypted_anthropic_api_key).decode()
    except InvalidToken:
        log.warning("claude_code.bootstrap_env.decrypt_failed")
        return
    log.info("claude_code.bootstrap_env.loaded")


def bootstrap() -> None:
    from app.core.byok import register_validator as _byok_register_validator  # noqa: PLC0415
    from app.domain.orgs import register_onboarding_contributor  # noqa: PLC0415
    from app.plugins.claude_code.byok_validator import validate_anthropic_key  # noqa: PLC0415
    from app.plugins.claude_code.installer import install_subagents  # noqa: PLC0415

    register_plugin(_plugin)
    register_onboarding_contributor("anthropic_key_set", _onboarding_anthropic_key_set)
    # BYOK: the `/api/api-keys/anthropic/validate` endpoint dispatches to this
    # callable so core/byok stays free of provider-specific HTTP.
    _byok_register_validator("anthropic", validate_anthropic_key)
    # Install yaaos-* subagent definitions so the parent reviewer can dispatch
    # them via the Task tool. Static files, idempotent — fine to run on every
    # backend startup. Future Docker-workspace isolation will move this per-
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
