"""Claude Code CLI wrapper. Implements `domain/coding_agent.CodingAgentPlugin`.

Vendor-only: this module talks to Anthropic's Claude Code CLI and nothing else.

Test-mode (stub/replay) wrapping is handled by the `testing/` layer's
`StubCodingAgentPlugin` — see `app.testing.stub_coding_agent`. The bootstrap
in `app/web.py` swaps real plugins for stubs when `YAAOS_CODING_AGENT_STUB`
is set; this file never branches on it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx
import structlog
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.core import byok as _byok
from app.core.auth import org_id_var
from app.core.coding_agent import (
    ActivityEvent,
    ActivityLog,
    CodingAgentError,
    ExecSpec,
    HealthStatus,
    InvokeCodingAgent,
    ReportedFinding,
    ReviewContext,
    RunResult,
    Usage,
    ValidationResult,
    register_plugin,
)
from app.core.coding_agent import (
    FindingDraftList as _FindingDraftList,
)
from app.core.coding_agent import (
    Invocation as _NewInvocation,
)
from app.core.config import get_settings
from app.core.database import session as db_session
from app.plugins.claude_code.models import ClaudeCodeSettingsRow

log = structlog.get_logger("claude_code")


# Default wallclock limit for a remote InvokeClaudeCode command. Big PRs with
# parallel subagent dispatch + per-finding verification can legitimately take
# 10-15 min on first run; 20 min gives headroom.
_DEFAULT_TIMEOUT_SECONDS = 1200

# Hardcoded model + effort for the remote invocation. Future UI work moves
# these to a settings row + per-job override. `--model opus` resolves to the
# latest Opus alias; `--effort medium` is a Claude Code reasoning level.
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
        log.debug(
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
                log.debug(
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
                log.debug(
                    "claude_code.stream.tool_result",
                    tool_use_id=block.get("tool_use_id"),
                    is_error=block.get("is_error", False),
                    excerpt=summary[:200],
                )
    elif et == "result":
        log.debug(
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


def _compute_state_v2(findings: list[ReportedFinding]) -> Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"]:
    """Severity tiers — only `blocker` requests changes."""
    if not findings:
        return "APPROVED"
    if any(f.severity == "blocker" for f in findings):
        return "CHANGES_REQUESTED"
    return "COMMENT"


# ── Plugin ────────────────────────────────────────────────────────────────────


class ClaudeCodePlugin:
    plugin_id = "claude_code"

    def install_url(self, org_id: UUID) -> str | None:
        """No out-of-band install — Claude Code settings are pure form. The
        bespoke settings page handles it."""
        del org_id
        return None

    def validate_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Pydantic validation: `{mcp_proxy_ids}` shape.
        See `settings_schema.validate_settings`. Accepts an empty dict."""
        from app.plugins.claude_code.settings_schema import (  # noqa: PLC0415
            validate_settings as _validate,
        )

        return _validate(settings)

    async def _load_settings_for_invocation(self, org_id: UUID) -> tuple[SecretStr | None, str | None]:
        """Returns (api_key, cli_path). API key read from byok_keys; cli_path from claude_code_settings."""
        async with db_session() as s:
            plaintext = await _byok.get(org_id, "anthropic", session=s)
            row = (
                await s.execute(select(ClaudeCodeSettingsRow).where(ClaudeCodeSettingsRow.org_id == org_id))
            ).scalar_one_or_none()
        api_key = SecretStr(plaintext) if plaintext else None
        return api_key, row.cli_path if row else None

    # ── Remote-dispatch methods ───────────────────────────────────────────

    async def build_review_invocation(
        self,
        ctx: ReviewContext,
        *,
        session: Any,
    ) -> Any:
        """Build the `Invocation` for a remote PR review.

        Resolves the per-repo skill name from `claude_code_repos`, reads
        the Anthropic key from `byok_keys`, assembles the prompt
        (review instructions + output schema appendix), and returns the
        complete exec spec. The Anthropic key goes in `exec.env` — the
        accepted carve-out for wire-bound exec.

        Raises `CodingAgentError` when the skill name is unconfigured or the
        Anthropic key is absent — the review step fails cleanly before dispatch.

        Returns a `_LegacyInvocation` (internal to `core/coding_agent`) — the
        type is `Any` here so the plugin layer does not import the private class
        across the module boundary.
        """
        from app.core.agent_gateway import InvokeClaudeCodeLimits  # noqa: PLC0415
        from app.core.coding_agent import _LegacyInvocation as _Inv  # noqa: PLC0415
        from app.plugins.claude_code.repos import resolve_skill  # noqa: PLC0415

        skill_name = await resolve_skill(ctx.org_id, ctx.repo_external_id, session=session)
        if not skill_name:
            raise CodingAgentError(
                f"skill_name not configured for repo {ctx.repo_external_id!r} — "
                "set it in Coding Agents settings before dispatching a review"
            )

        api_key, cli_path_setting = await self._load_settings_for_invocation(ctx.org_id)
        if not api_key:
            raise CodingAgentError("ANTHROPIC_API_KEY not set — add it in Org Settings → Coding Agents")
        cli_path = cli_path_setting or "claude"

        # Assemble the review prompt + the canonical output-schema appendix.
        # `ctx.output_schema` is the snapshot frozen at dispatch; we pass it
        # directly so the prompt matches what the caller will validate against.
        import json as _json  # noqa: PLC0415

        schema_str = _json.dumps(ctx.output_schema, indent=2)
        prompt = (
            f"Review the pull request. Base SHA: {ctx.base_sha}. Head SHA: {ctx.head_sha}.\n"
            f"Run `git diff {ctx.base_sha}..HEAD` to inspect changes.\n\n"
            "## Output Format (STRICT)\n\n"
            "Respond with EXACTLY a JSON object matching this schema. No markdown fences. "
            "No commentary. No preamble. Your response must start with `{` and end with `}`.\n\n"
            f"{schema_str}\n"
        )

        argv = (
            cli_path,
            "--print",
            "--output-format=stream-json",
            "--verbose",
            "--permission-mode=bypassPermissions",
            "--model",
            _MODEL,
            "--effort",
            _EFFORT,
            "--allowed-tools="
            "Read,Glob,Grep,LS,NotebookRead,TodoWrite,WebFetch,WebSearch,Task,"
            "Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git blame:*),"
            "Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git status)",
        )
        env = {"ANTHROPIC_API_KEY": api_key.get_secret_value()}

        return _Inv(
            kind=skill_name,
            exec=ExecSpec(argv=argv, stdin=prompt, env=env),
            limits=InvokeClaudeCodeLimits(wallclock_seconds=_DEFAULT_TIMEOUT_SECONDS),
        )

    def parse_review_output(self, stdout: str) -> list[ReportedFinding]:
        """Parse the agent's stream-json stdout into `ReportedFinding` objects.

        Finds the terminal `type=result` event, extracts the `result` field,
        and parses the JSON payload against `FindingDraftList`. Raises
        `ValueError` on any parse failure so `PostFindings` can gate on it.
        """
        events = _parse_stream_events(stdout)
        result_event = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result_event is None:
            raise ValueError("no 'type=result' event found in stdout")
        raw_result = result_event.get("result", "")
        if not isinstance(raw_result, str):
            raise ValueError(f"result field is not a string: {type(raw_result)}")
        try:
            parsed_dict = json.loads(raw_result)
            parsed = _FindingDraftList.model_validate(parsed_dict)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"agent output did not match FindingDraftList: {exc}") from exc
        return [
            ReportedFinding(
                file=d.file,
                line=d.line,
                category=d.category,
                severity=d.severity,
                confidence=d.confidence,
                rationale=d.rationale,
                rule_violated=d.rule_violated,
                rule_source=d.rule_source,
                suggested_fix=d.suggested_fix,
            )
            for d in parsed.findings
        ]

    async def review_preflight_steps(
        self,
        ctx: ReviewContext,
        *,
        session: Any,
    ) -> tuple[str, ...]:
        """Return WorkflowCommand kinds to insert before the review step.

        Returns `()` — the per-repo skill name model has no preflight steps.
        """
        del ctx, session
        return ()

    def parse_usage(self, stdout: str) -> Usage:
        """Extract token usage + duration from the terminal `type=result` event.

        Reads the last `type=result` event and pulls `usage.input_tokens`,
        `usage.output_tokens`, and `duration_ms`. Missing fields surface
        as `None`. A stream with no terminal `result` event returns an
        empty `Usage()` — never raises so callers can finalize a run row
        even when the agent crashed mid-stream.
        """
        events = _parse_stream_events(stdout)
        result_event = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result_event is None:
            return Usage()
        usage_blob = result_event.get("usage") or {}
        tokens_in: int | None = None
        tokens_out: int | None = None
        if isinstance(usage_blob, dict):
            raw_in = usage_blob.get("input_tokens")
            raw_out = usage_blob.get("output_tokens")
            if isinstance(raw_in, int):
                tokens_in = raw_in
            if isinstance(raw_out, int):
                tokens_out = raw_out
        duration_raw = result_event.get("duration_ms")
        duration_ms: int | None = duration_raw if isinstance(duration_raw, int) else None
        return Usage(tokens_in=tokens_in, tokens_out=tokens_out, duration_ms=duration_ms)

    def render_activity(self, stdout: str) -> ActivityLog:
        """Pre-render the full activity stream from terminal stdout.

        Walks every parseable stream-json event, drops null renders (events
        with no useful UI representation), and assigns monotonic `seq`
        starting from 0. Returns an empty `ActivityLog` for stdout with
        no parseable events.
        """
        events = _parse_stream_events(stdout)
        rendered: list[ActivityEvent] = []
        seq = 0
        for raw_event in events:
            ev = _render_activity(raw_event)
            if ev is None:
                continue
            # `_render_activity` returns a new ActivityEvent with `seq=0`
            # (the default); stamp the monotonic index here.
            rendered.append(ev.model_copy(update={"seq": seq}))
            seq += 1
        return ActivityLog(events=tuple(rendered))

    # ── New generic Protocol methods ─────────────────────────────────────

    def build_invocation(self, invocation: _NewInvocation) -> InvokeCodingAgent:
        """Translate a high-level `Invocation` into a concrete exec block.

        Delegates argv/stdin/env composition to the existing `_exec_block`
        helper in `core/coding_agent/invocation.py` via a synthetic
        `ReviewContext`-shaped dict from `invocation.context` for the
        `pr_review` skill. Raises `CodingAgentError` for unknown skills or
        when the context dict lacks required keys.
        """
        if invocation.skill != "pr_review":
            raise CodingAgentError(
                f"ClaudeCodePlugin does not support skill {invocation.skill!r}; only 'pr_review' is supported"
            )
        from pydantic import SecretStr as _SecretStr  # noqa: PLC0415

        from app.core.coding_agent import ReviewContext as _ReviewContext  # noqa: PLC0415
        from app.core.coding_agent import build_invocation as _build_inv  # noqa: PLC0415

        # Build a ReviewContext-compatible object from the generic context dict.
        # The invocation.context for pr_review carries the same keys as
        # ReviewContext (org_id, repo_external_id, pr_external_id, head_sha,
        # base_sha, output_schema). We validate required keys here.
        ctx_dict = invocation.context
        required = {"head_sha", "base_sha", "pr_external_id", "repo_external_id", "org_id"}
        missing = required - set(ctx_dict)
        if missing:
            raise CodingAgentError(
                f"build_invocation: context missing required keys for pr_review: {sorted(missing)}"
            )
        review_ctx = _ReviewContext(
            org_id=ctx_dict["org_id"],
            repo_external_id=ctx_dict["repo_external_id"],
            pr_external_id=ctx_dict["pr_external_id"],
            head_sha=ctx_dict["head_sha"],
            base_sha=ctx_dict["base_sha"],
            output_schema=ctx_dict.get("output_schema", {}),
        )
        # Read the API key from the context dict if supplied (used in tests);
        # production callers supply it via invocation.context["anthropic_api_key"].
        raw_api_key: str | None = ctx_dict.get("anthropic_api_key")
        api_key = _SecretStr(raw_api_key) if raw_api_key else None
        # Delegate to the public build_invocation helper (core/coding_agent/invocation.py)
        # which assembles the full invocation dict including the exec block.
        inv_dict = _build_inv(
            mode="review",
            context=review_ctx,
            model=invocation.model,
            effort=invocation.effort,
            anthropic_api_key=api_key,
        )
        exec_block = inv_dict["exec"]
        return InvokeCodingAgent(
            argv=list(exec_block["argv"]),
            env=exec_block["env"],
            stdin=exec_block.get("stdin") or None,
            wallclock_seconds=invocation.wallclock_seconds,
        )

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        """Decode a terminal AgentEvent payload into a `RunResult`.

        Reads `terminal_event_payload["stdout"]` / `["exit_code"]`, delegates
        to `parse_usage` and `render_activity` to populate `usage` and
        `activity`. Sets `output = stdout`, `error_message = None` (the
        sink derives status from the wire `event_kind`, not from the payload).
        Never raises on missing keys — missing stdout is treated as an empty
        string; missing exit_code is None.
        """
        stdout: str = terminal_event_payload.get("stdout", "") or ""
        exit_code_raw = terminal_event_payload.get("exit_code")
        exit_code: int | None = exit_code_raw if isinstance(exit_code_raw, int) else None
        usage = self.parse_usage(stdout)
        activity = self.render_activity(stdout)
        return RunResult(
            output=stdout,
            error_message=None,
            usage=usage,
            duration_ms=usage.duration_ms,
            exit_code=exit_code,
            activity=activity,
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
        org_id = org_id_var.get()
        if org_id is None:
            return HealthStatus(healthy=False, message="no org context", checked_at=_utcnow())
        api_key, _ = await self._load_settings_for_invocation(org_id)
        if not api_key:
            return HealthStatus(healthy=False, message="anthropic api key not set", checked_at=_utcnow())
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


async def _probe_anthropic_auth(api_key: SecretStr) -> tuple[bool, str]:
    """Return (healthy, message). Cached for `_AUTH_TTL` per key fingerprint.

    In stub mode (`YAAOS_CODING_AGENT_STUB`), the e2e test stack has no
    outbound connectivity to `api.anthropic.com` — and shouldn't need it,
    since the stub agent never calls Anthropic anyway. Treat any non-empty
    key as authenticating cleanly so onboarding and `/api/claude_code/health`
    behave consistently with the rest of the stubbed pipeline.
    """
    if get_settings().yaaos_coding_agent_stub:
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

    "Set" means: there's a byok row AND the key actually authenticates.
    A saved-but-invalid key does not satisfy the prereq.
    The auth probe is cached (5-min TTL per fingerprint).
    """
    async with db_session() as s:
        plaintext = await _byok.get(org_id, "anthropic", session=s)
    if not plaintext:
        return False
    healthy, _ = await _probe_anthropic_auth(SecretStr(plaintext))
    return healthy


def bootstrap() -> None:
    from app.core.byok import register_validator as _byok_register_validator  # noqa: PLC0415
    from app.domain.orgs import register_onboarding_contributor  # noqa: PLC0415
    from app.plugins.claude_code.byok_validator import validate_anthropic_key  # noqa: PLC0415

    register_plugin(_plugin)
    register_onboarding_contributor("anthropic_key_set", _onboarding_anthropic_key_set)
    # BYOK: the `/api/api-keys/anthropic/validate` endpoint dispatches to this
    # callable so core/byok stays free of provider-specific HTTP.
    _byok_register_validator("anthropic", validate_anthropic_key)


def get_plugin() -> ClaudeCodePlugin:
    return _plugin
