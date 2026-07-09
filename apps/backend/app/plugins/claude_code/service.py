"""Claude Code CLI wrapper. Implements `core/coding_agent.CodingAgentPlugin`.

Vendor-only: this module talks to Anthropic's Claude Code CLI and nothing else.

Test-mode (stub/replay) wrapping is handled by the `testing/` layer's
`StubCodingAgentPlugin` — see `app.testing.stub_coding_agent`. The bootstrap
in `app/web.py` swaps real plugins for stubs when `YAAOS_CODING_AGENT_STUB`
is set; this file never branches on it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import structlog
from pydantic import SecretStr

import app.core.api_keys as _api_keys
from app.core.coding_agent import (
    ActivityEvent,
    ActivityLog,
    CodingAgentError,
    InvokeCodingAgent,
    RunResult,
    Usage,
    register_plugin,
)
from app.core.coding_agent import (
    Invocation as _NewInvocation,
)
from app.core.config import get_settings
from app.core.database import session as db_session
from app.plugins.claude_code.settings_schema import validate_settings as _validate_settings

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


def _render_activity(event: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one Claude Code stream event into a user-facing activity dict.

    Returns `None` for events with no useful render (e.g. unknown types, empty
    assistant turns). The `message` is pre-rendered for direct UI display; raw
    event data lands in `detail` for the expanded view.

    The returned dict matches the shape expected by `ActivityEvent`:
    `{seq, ts, kind, message, detail}`. `seq` is assigned monotonically by
    `_render_activity_log` after filtering null renders. `ts` is a `datetime`
    object — `_render_activity_log` constructs the typed `ActivityEvent`.
    """
    et = event.get("type")
    ts = _utcnow()
    if et == "system" and event.get("subtype") == "init":
        model = event.get("model") or "?"
        return {
            "seq": 0,
            "ts": ts,
            "kind": "session_start",
            "message": f"Session started · model {model}",
            "detail": {"model": model, "session_id": event.get("session_id")},
        }
    if et == "assistant":
        msg = event.get("message", {}) or {}
        # An assistant turn may contain a mix of text + tool_use blocks. Render
        # them in order — emit the first block we can, since one activity event
        # per stream event keeps the feed cardinality 1:1 with stream lines.
        for block in msg.get("content", []) or []:
            btype = block.get("type")
            if btype == "tool_use":
                tool = block.get("name") or "?"
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                if tool == "Task":
                    subagent = inp.get("subagent_type") or "subagent"
                    return {
                        "seq": 0,
                        "ts": ts,
                        "kind": "subagent_dispatched",
                        "message": f"Dispatching {subagent}",
                        "detail": {
                            "subagent": subagent,
                            "tool_use_id": block.get("id"),
                            "description": inp.get("description"),
                        },
                    }
                # Other tool calls — Read, Bash, Grep, Glob, etc.
                target = _summarize_tool_input(tool, inp)
                return {
                    "seq": 0,
                    "ts": ts,
                    "kind": "tool_call_started",
                    "message": f"{tool}: {target}" if target else tool,
                    # Trust-boundary: activity events cross from the customer's
                    # workspace to yaaos' control plane. `inp` for Edit / Write
                    # tools carries the full source content the agent is about
                    # to commit; we MUST NOT leak it across the boundary. Only
                    # metadata fields (paths, command summaries) are kept.
                    "detail": {
                        "tool": tool,
                        "tool_use_id": block.get("id"),
                        "input_summary": _safe_tool_input(tool, inp),
                    },
                }
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    excerpt = text if len(text) < 200 else text[:197] + "…"
                    return {
                        "seq": 0,
                        "ts": ts,
                        "kind": "assistant_message",
                        "message": excerpt,
                        "detail": {},
                    }
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
            return {
                "seq": 0,
                "ts": ts,
                "kind": "tool_call_finished",
                "message": message,
                "detail": {
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": is_error,
                    "size_bytes": size_bytes,
                },
            }
        return None
    if et == "result":
        return {
            "seq": 0,
            "ts": ts,
            "kind": "result",
            "message": "Review complete",
            "detail": {
                "duration_ms": event.get("duration_ms"),
                "num_turns": event.get("num_turns"),
            },
        }
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


# ── Parse helpers (used by build_invocation + parse_result) ──────────────────


def _parse_usage(stdout: str) -> Usage:
    """Extract token usage from the terminal `type=result` event.

    Reads the last `type=result` event and pulls `usage.input_tokens`
    and `usage.output_tokens`. Missing fields surface as `None`. A
    stream with no terminal `result` event returns an empty `Usage()` —
    never raises.
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
    return Usage(tokens_in=tokens_in, tokens_out=tokens_out)


def _render_activity_log(stdout: str) -> ActivityLog:
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
        # `_render_activity` returns a dict with `seq=0` (placeholder);
        # stamp the monotonic index and construct the typed ActivityEvent here.
        rendered.append(ActivityEvent(**{**ev, "seq": seq}))
        seq += 1
    return ActivityLog(events=rendered)


# ── Stage-invocation prompt rendering ─────────────────────────────────────────
#
# Renders `Invocation.context` (a `domain/pipelines.StageInvocationContext`
# dump plus the engine-injected `output_schema`) into the headless prompt a
# pipeline skill stage runs against. The skill file itself
# (`.claude/skills/<skill>/SKILL.md`) owns every skill-specific instruction —
# this rendering only supplies the generic stage context every invocation
# carries, so the plugin stays skill-agnostic.


def _render_stage_prompt(skill: str, ctx: Mapping[str, Any]) -> str:
    lines: list[str] = [
        f'Use the "{skill}" skill (.claude/skills/{skill}/SKILL.md) to complete this pipeline stage.'
    ]

    stage_name = ctx.get("stage_name")
    ticket_id = ctx.get("ticket_id")
    header = f"\nStage: {stage_name}" if stage_name else ""
    if header and ticket_id:
        header += f" (ticket {ticket_id})"
    if header:
        lines.append(header)

    lines.append("\n## Input\n")
    lines.append(str(ctx.get("input") or ""))

    pr = ctx.get("pr")
    if pr:
        lines.append("\n## Pull request\n")
        lines.append(f"- PR: {pr['pr_external_id']}")
        lines.append(f"- Base SHA: {pr['base_sha']}")
        lines.append(f"- Head SHA: {pr['head_sha']}")
        prev = pr.get("prev_reviewed_head_sha")
        lines.append(f"- Previously reviewed head SHA: {prev or 'none (first review)'}")
        diff_base = prev or pr["base_sha"]
        lines.append(f"\nRun `git diff {diff_base}..{pr['head_sha']}` to inspect the change.")

    upstream_stages = ctx.get("upstream_stages") or []
    if upstream_stages:
        lines.append("\n## Upstream artifacts\n")
        for stage in upstream_stages:
            lines.append(f"### {stage['stage_name']} — {stage['description']}\n")
            lines.append(stage["artifact_body"])

    revision = ctx.get("revision")
    if revision:
        source_label = {
            "instruction": "Human instruction",
            "send_back": "Send-back gap",
            "fix": "Fix request",
        }.get(revision["source"], revision["source"])
        lines.append(f"\n## Revision ({source_label})\n")
        lines.append(revision["text"])
        lines.append("\n### Prior artifact\n")
        lines.append(revision["prior_artifact"])

    prior_findings = ctx.get("prior_findings") or []
    if prior_findings:
        lines.append("\n## Prior findings\n")
        for finding in prior_findings:
            if finding.get("code_file"):
                loc = f" ({finding['code_file']}:{finding.get('code_line') or '?'})"
            elif finding.get("artifact_section"):
                loc = f" ({finding['artifact_section']})"
            else:
                loc = ""
            lines.append(f"- [{finding['finding_id']}] [{finding['severity']}]{loc} {finding['body']}")

    artifact_path = ctx.get("artifact_path")
    lines.append("\n## Output\n")
    if artifact_path:
        lines.append(f"Write your artifact output to `{artifact_path}`.\n")

    schema_str = json.dumps(ctx.get("output_schema", {}), indent=2)
    lines.append(
        "Respond with EXACTLY a JSON object matching this schema. No markdown fences. "
        "No commentary. No preamble. Your response must start with `{` and end with `}`.\n\n"
        f"{schema_str}"
    )
    return "\n".join(lines)


# ── Plugin ────────────────────────────────────────────────────────────────────


class ClaudeCodePlugin:
    plugin_id = "claude_code"

    def api_key_requirement(self) -> str | None:
        """Claude Code requires an Anthropic API key."""
        return "anthropic"

    def compile_invocation(self, invocation: _NewInvocation) -> InvokeCodingAgent:
        """Translate a high-level `Invocation` into a concrete exec block.

        Generic across every pipeline skill: the backend supplies the full
        `StageInvocationContext` (rendered into the prompt below) plus the
        engine-injected `output_schema`; the named skill file
        (`.claude/skills/<skill>/SKILL.md` in the checkout) owns all
        skill-specific instructions — this plugin has no per-skill knowledge.
        Raises `CodingAgentError` when the context is missing the fields
        every stage invocation carries.
        """
        ctx_dict = invocation.context
        required = {"stage_name", "input", "artifact_path"}
        missing = required - set(ctx_dict)
        if missing:
            raise CodingAgentError(f"compile_invocation: context missing required keys: {sorted(missing)}")

        prompt = _render_stage_prompt(invocation.skill, ctx_dict)

        argv = [
            "claude",
            "--print",
            "--output-format=stream-json",
            "--verbose",
            "--permission-mode=bypassPermissions",
            "--model",
            invocation.model,
            "--effort",
            invocation.effort,
        ]

        return InvokeCodingAgent(
            argv=argv,
            env={},
            stdin=prompt,
            wallclock_seconds=invocation.wallclock_seconds,
        )

    def validate_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize a raw settings dict via `ClaudeCodeSettings`.

        Delegates to `settings_schema.validate_settings`, which parses through
        `ClaudeCodeSettings(extra="forbid")`. Unknown keys raise `ValueError`.
        Returns the normalized dict suitable for persisting to
        `org_coding_agents.settings`.
        """
        return _validate_settings(dict(settings))

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        """Decode a terminal AgentEvent payload into a `RunResult`.

        Reads `terminal_event_payload["stdout"]` / `["exit_code"]`, delegates
        to `_parse_usage` and `_render_activity_log` to populate `usage` and
        `activity`. Reads `duration_ms` directly from the terminal `result`
        stream event. Sets `error_message = None` (the sink derives status
        from the wire `event_kind`, not from the payload). Never raises on
        missing keys — missing stdout is treated as an empty string; missing
        exit_code and duration_ms are None.
        """
        stdout: str = terminal_event_payload.get("stdout", "") or ""
        exit_code_raw = terminal_event_payload.get("exit_code")
        exit_code: int | None = exit_code_raw if isinstance(exit_code_raw, int) else None
        usage = _parse_usage(stdout)
        activity = _render_activity_log(stdout)
        # Read duration_ms directly from the terminal result event — it lives
        # on the result event itself, not inside the usage sub-object.
        events = _parse_stream_events(stdout)
        result_event = next((e for e in reversed(events) if e.get("type") == "result"), None)
        duration_ms: int | None = None
        if result_event is not None:
            duration_raw = result_event.get("duration_ms")
            if isinstance(duration_raw, int):
                duration_ms = duration_raw
        # Extract the structured response from the terminal result event.
        # The agent emits its structured answer in result_event["result"] (the
        # content of the `--output-format=stream-json` result line). Downstream
        # `CodingAgentCommand.handle_response` validates this string directly
        # via `model_validate_json`. An empty fallback produces a validation
        # failure (retryable=False) rather than silent data loss.
        output: str = ""
        if result_event is not None:
            raw_result = result_event.get("result")
            if isinstance(raw_result, str):
                output = raw_result
        return RunResult(
            output=output,
            error_message=None,
            usage=usage,
            duration_ms=duration_ms,
            exit_code=exit_code,
            activity=activity,
        )

    def parse_activity_line(self, line: str) -> ActivityEvent | None:
        """Map one stream-json line from a live `progress` AgentEvent into a
        renderable `ActivityEvent`, reusing `_render_activity` — the exact
        per-line mapping `parse_result`/`_render_activity_log` apply at
        finalize, so live and persisted views never diverge in taxonomy.

        Returns `None` for unparseable JSON, a non-dict line, or a line with
        no useful render (same null cases as `_render_activity`). `seq` is
        always 0 — a single line carries no run-wide ordering context.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        rendered = _render_activity(event)
        if rendered is None:
            return None
        return ActivityEvent(**{**rendered, "seq": 0})


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
    key as authenticating cleanly so onboarding behaves consistently with
    the rest of the stubbed pipeline.
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

    "Set" means: there's an api_keys row AND the key actually authenticates.
    A saved-but-invalid key does not satisfy the prereq.
    The auth probe is cached (5-min TTL per fingerprint).
    """
    async with db_session() as s:
        plaintext = await _api_keys.get(org_id, "anthropic", session=s)
    if not plaintext:
        return False
    healthy, _ = await _probe_anthropic_auth(SecretStr(plaintext))
    return healthy


def bootstrap() -> None:
    from app.core.api_keys import register_validator as _api_keys_register_validator  # noqa: PLC0415
    from app.domain.orgs import register_onboarding_contributor  # noqa: PLC0415
    from app.plugins.claude_code.api_key_validator import validate_anthropic_key  # noqa: PLC0415

    register_plugin(_plugin)
    register_onboarding_contributor("anthropic_key_set", _onboarding_anthropic_key_set)
    # The `/api/api-keys/anthropic/validate` endpoint dispatches to this
    # callable so core/api_keys stays free of provider-specific HTTP.
    _api_keys_register_validator("anthropic", validate_anthropic_key)


def get_plugin() -> ClaudeCodePlugin:
    return _plugin


@contextmanager
def set_claude_code_plugin_for_tests(plugin: ClaudeCodePlugin | None = None) -> Iterator[ClaudeCodePlugin]:
    """Context manager: swap the singleton plugin for the duration of the block.

    Pass an explicit ``plugin`` instance or omit to receive a fresh default
    ``ClaudeCodePlugin``. Restores the prior singleton on exit — even on exception.

    Production never calls this.
    """
    global _plugin
    prior = _plugin
    _plugin = plugin if plugin is not None else ClaudeCodePlugin()
    try:
        yield _plugin
    finally:
        _plugin = prior
