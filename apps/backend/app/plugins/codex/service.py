"""Codex CLI wrapper. Implements `core/coding_agent.CodingAgentPlugin`.

Vendor-only: talks to OpenAI's Codex CLI and nothing else.

Parses JSONL events from `codex exec` stdout:
  - `item.completed` (assistant message) — final text response
  - `turn.completed` — per-run token usage
  - `turn.failed` / `error` — failure details
  - `item.created` (function_call) — tool call started
  - `item.completed` (function_call) — tool call finished
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
import yaml

from app.core.coding_agent import (
    ActivityEvent,
    ActivityLog,
    AgentSource,
    BundleFile,
    InvokeCodingAgent,
    RunResult,
    SkillSource,
    StageOptions,
    Usage,
    register_plugin,
)
from app.core.coding_agent import (
    Invocation as _Invocation,
)
from app.domain.pipelines import render_stage_prompt
from app.plugins.codex.defaults import EFFORTS, MODELS

log = structlog.get_logger("codex")


# ── JSONL event helpers ───────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_jsonl_events(stdout: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON events from `codex exec` stdout.

    Invalid lines are silently skipped — a single malformed line must not
    interrupt the parse pass.
    """
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


def _extract_final_text(events: list[dict[str, Any]]) -> str:
    """Return the assistant's final text from `item.completed` events.

    Joins multiple `output_text` content parts in the last assistant message.
    Empty string when no assistant message appears.
    """
    text_parts: list[str] = []
    for ev in events:
        if ev.get("type") != "item.completed":
            continue
        item = ev.get("item") or {}
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        parts: list[str] = []
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text") or ""
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            # Last assistant message wins; overwrite earlier partial messages.
            text_parts = parts
    return "".join(text_parts)


def _extract_usage(events: list[dict[str, Any]]) -> Usage:
    """Return token usage from the first `turn.completed` event."""
    for ev in events:
        if ev.get("type") == "turn.completed":
            usage_raw = ev.get("usage") or {}
            if isinstance(usage_raw, dict):
                tokens_in_raw = usage_raw.get("input_tokens")
                tokens_out_raw = usage_raw.get("output_tokens")
                return Usage(
                    tokens_in=tokens_in_raw if isinstance(tokens_in_raw, int) else None,
                    tokens_out=tokens_out_raw if isinstance(tokens_out_raw, int) else None,
                )
    return Usage()


def _render_activity_codex(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Map one codex JSONL event to a pre-rendered activity dict, or None.

    Only the subset of events with a meaningful human-readable render is
    returned; internal/debug events are discarded.
    """
    ts = _utcnow().isoformat()
    etype = ev.get("type") or ""

    if etype == "turn.started":
        return {
            "seq": 0,
            "ts": ts,
            "kind": "session_start",
            "message": "Codex turn started",
            "detail": {"model": None, "session_id": None},
        }

    if etype == "item.created":
        item = ev.get("item") or {}
        itype = item.get("type") or ""
        if itype == "function_call":
            tool = item.get("name") or "tool"
            call_id = item.get("call_id") or ""
            args_raw = item.get("arguments") or ""
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) and args_raw else {}
            except json.JSONDecodeError:
                args = {}
            target = _summarize_codex_tool_input(tool, args)
            return {
                "seq": 0,
                "ts": ts,
                "kind": "tool_call_started",
                "message": f"{tool}: {target}" if target else tool,
                "detail": {
                    "tool": tool,
                    "tool_use_id": call_id,
                    "input_summary": _safe_codex_tool_input(tool, args),
                },
            }
        return None

    if etype == "item.completed":
        item = ev.get("item") or {}
        itype = item.get("type") or ""
        if itype == "message" and item.get("role") == "assistant":
            content = item.get("content") or []
            text_parts = [
                b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "output_text"
            ]
            text = "".join(text_parts).strip()
            excerpt = text if len(text) < 200 else text[:197] + "…"
            if not excerpt:
                return None
            return {
                "seq": 0,
                "ts": ts,
                "kind": "assistant_message",
                "message": excerpt,
                "detail": {},
            }
        if itype == "function_call":
            call_id = item.get("call_id") or ""
            return {
                "seq": 0,
                "ts": ts,
                "kind": "tool_call_finished",
                "message": "→ ok",
                "detail": {
                    "tool_use_id": call_id,
                    "is_error": False,
                    "size_bytes": 0,
                },
            }
        if itype == "function_call_output":
            call_id = item.get("call_id") or ""
            output = item.get("output") or ""
            size_bytes = len(str(output).encode())
            return {
                "seq": 0,
                "ts": ts,
                "kind": "tool_call_finished",
                "message": f"→ ok ({size_bytes} bytes)" if size_bytes else "→ ok (empty)",
                "detail": {
                    "tool_use_id": call_id,
                    "is_error": False,
                    "size_bytes": size_bytes,
                },
            }
        return None

    if etype == "turn.completed":
        usage_raw = ev.get("usage") or {}
        tokens_in = usage_raw.get("input_tokens") if isinstance(usage_raw, dict) else None
        tokens_out = usage_raw.get("output_tokens") if isinstance(usage_raw, dict) else None
        detail: dict[str, Any] = {"duration_ms": None, "num_turns": None}
        if isinstance(tokens_in, int):
            detail["tokens_in"] = tokens_in
        if isinstance(tokens_out, int):
            detail["tokens_out"] = tokens_out
        return {
            "seq": 0,
            "ts": ts,
            "kind": "result",
            "message": "Codex turn completed",
            "detail": detail,
        }

    if etype in ("turn.failed", "error"):
        err = ev.get("error") or {}
        msg = (err.get("message") or str(err)) if isinstance(err, dict) else str(err)
        return {
            "seq": 0,
            "ts": ts,
            "kind": "result",
            "message": f"Error: {msg}" if msg else "Codex error",
            "detail": {"duration_ms": None, "num_turns": None},
        }

    return None


def _summarize_codex_tool_input(tool: str, inp: dict[str, Any]) -> str:
    """One-line summary of a codex tool call's arguments for the activity feed."""
    if "path" in inp:
        return str(inp["path"])
    if "command" in inp:
        cmd = str(inp["command"])
        return cmd if len(cmd) < 120 else cmd[:117] + "…"
    if "query" in inp:
        return str(inp["query"])
    if "url" in inp:
        return str(inp["url"])
    return ""


def _safe_codex_tool_input(tool: str, inp: dict[str, Any]) -> dict[str, Any]:
    """Metadata-only projection safe to ship in ActivityEvent.detail."""
    if "path" in inp:
        return {"path": str(inp["path"])}
    if "command" in inp:
        cmd = str(inp["command"])
        return {"command_prefix": cmd if len(cmd) < 120 else cmd[:117] + "…"}
    if "query" in inp:
        return {"query": str(inp["query"])}
    if "url" in inp:
        return {"url": str(inp["url"])}
    return {}


def _render_activity_log(events: list[dict[str, Any]]) -> ActivityLog:
    """Build a pre-rendered `ActivityLog` from a codex JSONL event list."""
    rendered: list[ActivityEvent] = []
    seq = 0
    for ev in events:
        pre = _render_activity_codex(ev)
        if pre is None:
            continue
        rendered.append(ActivityEvent(**{**pre, "seq": seq}))
        seq += 1
    return ActivityLog(events=rendered)


# ── Plugin ────────────────────────────────────────────────────────────────────


class CodexPlugin:
    plugin_id = "codex"
    display_name = "Codex"
    command_kind = "InvokeCodex"

    def stage_options(self) -> StageOptions:
        """Return the Codex model and effort enumerations.

        Sourced from `defaults.py` constants. Pure — no IO.
        """
        return StageOptions(models=MODELS, efforts=EFFORTS)

    def skill_path(self, skill_name: str) -> str:
        """Return the checkout-relative Codex skill path for `skill_name`.

        Convention: `.codex/skills/<skill_name>/SKILL.md`.
        The agent stats this path before spawning codex; absent → failure.
        """
        return f".codex/skills/{skill_name}/SKILL.md"

    def compile_invocation(self, invocation: _Invocation) -> InvokeCodingAgent:
        """Translate a high-level `Invocation` into a concrete exec block.

        Passes the rendered stage prompt to `codex exec` via stdin. Sets
        `output_schema_json` from `context["output_schema"]` when present so
        the Go agent can write it to `$TMPDIR/<command_id>-schema.json` and
        append `--output-schema <path>` to argv before spawning.
        """
        skill_directive = (
            f'Use the "{invocation.skill}" skill ({self.skill_path(invocation.skill)}) '
            f"to complete this pipeline stage."
        )
        # Codex agents can spawn sub-agents — the delegation-authorization
        # sentence is required so the skill's sub-agent invocations are not
        # rejected by the backend's authorization checks.
        extra_directives = [
            "You are authorized to use sub-agents and delegate tasks to other agents "
            "as needed by the skill instructions."
        ]
        prompt = render_stage_prompt(
            invocation.context,
            skill_directive=skill_directive,
            extra_directives=extra_directives,
            output_schema_mode="native",
        )

        argv = ["codex", "exec", "--model", invocation.model, "--quiet"]

        # output_schema_json is written to $TMPDIR/<command_id>-schema.json and
        # --output-schema <path> appended by the Go agent's RunCodex, not here.
        output_schema_raw = invocation.context.get("output_schema")
        output_schema_json: str | None = None
        if output_schema_raw is not None:
            if isinstance(output_schema_raw, str):
                output_schema_json = output_schema_raw
            else:
                output_schema_json = json.dumps(output_schema_raw)

        return InvokeCodingAgent(
            argv=argv,
            env={},
            stdin=prompt,
            wallclock_seconds=invocation.wallclock_seconds,
            output_schema_json=output_schema_json,
        )

    def validate_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize a raw settings dict.

        Accepted keys: `auth_mode` (one of `"api_key"`, `"per_user"`).
        Raises `ValueError` for unknown keys or invalid values.
        """
        allowed_keys = {"auth_mode"}
        unknown = set(settings.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"unexpected codex settings keys: {sorted(unknown)}")
        auth_mode = settings.get("auth_mode")
        if auth_mode not in ("api_key", "per_user"):
            raise ValueError(f"codex auth_mode must be 'api_key' or 'per_user', got {auth_mode!r}")
        return {"auth_mode": auth_mode}

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        """Decode a terminal AgentEvent payload into a `RunResult`.

        Reads `stdout` (JSONL from `codex exec`) and `exit_code`.
        Extracts the final assistant message text as `output`, token usage
        from `turn.completed`, and builds the activity log. Never raises on
        missing or malformed keys.
        """
        stdout: str = terminal_event_payload.get("stdout", "") or ""
        exit_code_raw = terminal_event_payload.get("exit_code")
        exit_code: int | None = exit_code_raw if isinstance(exit_code_raw, int) else None

        events = _parse_jsonl_events(stdout)
        output = _extract_final_text(events)
        usage = _extract_usage(events)
        activity = _render_activity_log(events)

        return RunResult(
            output=output,
            error_message=None,
            usage=usage,
            duration_ms=None,  # codex does not report wall-clock in this field
            exit_code=exit_code,
            activity=activity,
        )

    def parse_activity_line(self, line: str) -> ActivityEvent | None:
        """Map one JSONL line from a live `progress` AgentEvent into an `ActivityEvent`.

        Returns `None` for unparseable JSON, a non-dict line, or a line with
        no useful render. `seq` is always 0 — a single line carries no
        run-wide ordering context.
        """
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(ev, dict):
            return None
        rendered = _render_activity_codex(ev)
        if rendered is None:
            return None
        return ActivityEvent(**{**rendered, "seq": 0})

    def render_skill_bundle(
        self,
        skills: Sequence[SkillSource],
        agents: Sequence[AgentSource],
    ) -> list[BundleFile]:
        """Render a codex-native skills bundle from canonical source objects.

        Produces:
        - `.codex/skills/<name>/SKILL.md` for each skill source plus any
          extra_files. Content is the same markdown as the canonical source
          (body and frontmatter preserved) — only the directory changes.
        - `.codex/agents/<name>.toml` for each agent source. The TOML carries
          the agent name, description, and the full instruction body; each
          body is prepended with a defensive task-restatement directive
          (mitigation for codex/codex-rs#27180, where spawned agents may
          ignore their task envelope).
        - `AGENTS.md` at repo root, carrying the delegation-authorization
          sentence using the exact trigger vocabulary that codex CLI ≥ 0.142.5
          recognises (PR #30274/#30511). Belt-and-braces alongside the per-stage
          authorization line emitted by `compile_invocation`.
        """
        files: list[BundleFile] = []

        for skill in skills:
            content = _reconstruct_md_codex(skill.frontmatter, skill.body)
            files.append(BundleFile(path=f".codex/skills/{skill.name}/SKILL.md", content=content))
            for ef in skill.extra_files:
                # Remap extra files from .claude/ → .codex/ tree.
                codex_path = ef.path.replace(".claude/skills/", ".codex/skills/", 1)
                files.append(BundleFile(path=codex_path, content=ef.content))

        for agent in agents:
            toml_content = _build_agent_toml(agent)
            files.append(BundleFile(path=f".codex/agents/{agent.name}.toml", content=toml_content))

        files.append(BundleFile(path="AGENTS.md", content=_AGENTS_MD))

        return files


# ── Codex-bundle helpers ──────────────────────────────────────────────────────

# Delegation-authorization sentence for the bundle's `AGENTS.md`.
# Uses the exact trigger vocabulary that codex CLI ≥ 0.142.5 recognises in its
# spawn-tool guardrail (PR #30274 + #30511): "applicable AGENTS.md/skill
# instructions" + "sub-agents, delegation, or parallel agent work".
_AGENTS_MD = """\
# AGENTS.md

These applicable AGENTS.md instructions explicitly authorize sub-agents,
delegation, and parallel agent work for pipeline stages that require it.
The skill instructions for each stage define which agents to invoke and when.
"""

# Defensive restatement directive prepended to each agent's instruction body.
# Mitigates codex/codex-rs#27180, where spawned agents occasionally ignore
# their task envelope.  This line is the first thing the spawned agent reads,
# prompting it to surface the deliverable before acting.
_DEFENSIVE_RESTATEMENT = (
    "> **Before taking any action, restate the specific deliverable from the task you received.**"
)


def _reconstruct_md_codex(frontmatter: dict[str, Any], body: str) -> str:
    """Reconstruct a markdown skill file with YAML frontmatter."""
    if frontmatter:
        yaml_text = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).rstrip()
        return f"---\n{yaml_text}\n---\n\n{body}\n"
    return f"{body}\n"


def _toml_str(value: str) -> str:
    """Format a Python string as a TOML basic string (double-quoted, escaped)."""
    # Escape backslashes first, then double quotes, then control characters.
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _build_agent_toml(agent: AgentSource) -> str:
    """Build a `.codex/agents/<name>.toml` file from an `AgentSource`.

    The TOML carries:
    - `name` and `description` from the frontmatter.
    - A `[prompt]` section with `content` = defensive restatement + body.

    Uses a literal TOML multi-line string (triple single-quotes) so the body
    markdown can be embedded verbatim without escaping special characters.
    Only `'''` sequences in the body (which would break the literal string)
    are substituted with `''\\'` as an escape.  This is an extremely rare
    edge case in practice; the canonical agent bodies contain none.
    """
    name = agent.frontmatter.get("name") or agent.name
    description = agent.frontmatter.get("description") or ""

    # Prepend defensive restatement to the body.
    body = f"{_DEFENSIVE_RESTATEMENT}\n\n{agent.body}".strip()

    # TOML literal multi-line strings: enclosed in ''', no escaping needed
    # except that ''' itself cannot appear inside.  Replace ''' with '' + '
    # (split across two literal strings) to avoid the sequence.
    safe_body = body.replace("'''", "'' '")

    lines = [
        f"name = {_toml_str(name)}",
        f"description = {_toml_str(description)}",
        "",
        "[prompt]",
        "content = '''",
        safe_body,
        "'''",
        "",
    ]
    return "\n".join(lines)


_plugin = CodexPlugin()


def bootstrap() -> None:
    from app.core.api_keys import register_validator as _api_keys_register_validator  # noqa: PLC0415
    from app.plugins.codex.api_key_validator import validate_openai_key  # noqa: PLC0415

    register_plugin(_plugin)
    _api_keys_register_validator("openai", validate_openai_key)


def get_plugin() -> CodexPlugin:
    return _plugin


@contextmanager
def set_codex_plugin_for_tests(plugin: CodexPlugin | None = None) -> Iterator[CodexPlugin]:
    """Context manager: swap the singleton plugin for the duration of the block.

    Pass an explicit ``plugin`` instance or omit to receive a fresh default
    ``CodexPlugin``. Restores the prior singleton on exit — even on exception.

    Production never calls this.
    """
    global _plugin
    prior = _plugin
    _plugin = plugin if plugin is not None else CodexPlugin()
    try:
        yield _plugin
    finally:
        _plugin = prior
