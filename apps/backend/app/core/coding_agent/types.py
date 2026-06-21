"""Types + Protocol for the coding-agent abstraction.

`CodingAgentPlugin` is the Protocol every coding-agent plugin must satisfy:
`compile_invocation` translates a high-level `Invocation` into a concrete
`InvokeCodingAgent` exec block; `parse_result` decodes a terminal AgentEvent
payload into a `RunResult`; `validate_settings` validates and normalizes a raw
settings dict. Plugins own skill resolution, model mapping, stdout parsing, and
settings validation; `core/coding_agent` owns dispatch and the run lifecycle.

`ReviewContext`, `ReportedFindingShape`, and `CodeReviewResponse` live in
`domain/reviewer` — they are reviewer-domain types, not generic coding-agent types.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

# Plugin/model-specific effort level string. The plugin validates its
# own allowed values; `core/coding_agent` treats this as an opaque str.
Effort = str


class Invocation(BaseModel):
    """High-level intent passed to `CodingAgentPlugin.compile_invocation`.

    Carries the workspace the coding-agent runs in, the skill name, model,
    effort level, generic context dict (plugin interprets the keys for the
    skill), and the wallclock cap. `workspace_id` identifies the workspace row
    that owns this invocation — `CodingAgentCommand.@final dispatch` reads it
    here rather than via an untyped attribute access on the inputs object.
    Pure data — no exec block. `compile_invocation` translates this into
    an `InvokeCodingAgent` with the concrete argv/env/stdin the agent runs.
    """

    workspace_id: UUID
    skill: str
    model: str
    effort: Effort
    context: Mapping[str, Any]
    wallclock_seconds: int


class InvokeCodingAgent(BaseModel):
    """Concrete exec block returned by `CodingAgentPlugin.compile_invocation`.

    Carries the exact argv, env overrides, optional stdin, and wallclock
    cap the Go agent uses to spawn the Claude Code subprocess.
    `env` carries the Anthropic API key — the accepted carve-out for
    wire-bound exec (same contract as the `otlp_token` on ConfigUpdate).
    """

    argv: list[str]
    env: Mapping[str, str]
    stdin: str | None = None
    wallclock_seconds: int


class RunStatus(StrEnum):
    """Terminal state of a coding-agent run as reported by the wire event_kind.

    Set by the run sink from the wire `event_kind`, NOT by the plugin.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class Usage(BaseModel):
    """Per-run token usage.

    Populated by `CodingAgentPlugin.parse_result` from the terminal
    stream-json event. Persisted onto `coding_agent_runs` by `finalize_run`.
    Fields default to None when the agent didn't report them.
    Wall-clock duration lives on `RunResult.duration_ms`, not here.
    """

    tokens_in: int | None = None
    tokens_out: int | None = None


# Canonical set of kind values the producer (`plugins/claude_code._render_activity`)
# emits. Exposed so tests and docs can reference the authoritative tuple.
ACTIVITY_EVENT_KINDS = frozenset(
    {
        "session_start",  # system.init — model + session established
        "subagent_dispatched",  # assistant.tool_use with tool == "Task"
        "tool_call_started",  # assistant.tool_use with any other tool
        "assistant_message",  # assistant.text block
        "tool_call_finished",  # user.tool_result block
        "result",  # terminal result event
    }
)

ActivityEventKind = Literal[
    "session_start",
    "subagent_dispatched",
    "tool_call_started",
    "assistant_message",
    "tool_call_finished",
    "result",
]


class ActivityEvent(BaseModel):
    """One rendered event in a coding-agent activity stream.

    Produced by `plugins/claude_code._render_activity_log` from Claude Code
    stream-json events. Persisted inside the `ActivityLog.events` JSONB array.

    Fields:
    - `seq` — monotonic integer; 0-based; assigned by `_render_activity_log`.
    - `ts` — UTC datetime of the render pass (post-hoc, not real-time stream).
      Pydantic coerces ISO-8601 strings to `datetime` on construction.
    - `kind` — one of the six canonical values in `ActivityEventKind`.
    - `message` — pre-rendered one-liner for the SPA activity feed.
    - `detail` — kind-specific metadata dict (safe for cross-boundary transport).

    Per-kind `detail` shapes:
    - `session_start`: `{model: str, session_id: str | None}`
    - `subagent_dispatched`: `{subagent: str, tool_use_id: str, description: str | None}`
    - `tool_call_started`: `{tool: str, tool_use_id: str, input_summary: dict}`
    - `assistant_message`: `{}` (message carries the text excerpt)
    - `tool_call_finished`: `{tool_use_id: str, is_error: bool, size_bytes: int}`
    - `result`: `{duration_ms: int | None, num_turns: int | None}`
    """

    seq: int
    ts: datetime
    kind: ActivityEventKind
    message: str
    detail: dict[str, Any] = {}


class ActivityLog(BaseModel):
    """Pre-rendered activity stream for one coding-agent run.

    Produced by `CodingAgentPlugin.parse_result` — the same event sequence
    the in-process path streams via `OnActivity`, captured durably for the
    Activity tab. Persisted as a JSONB blob in the partitioned
    `coding_agent_activity` table.

    JSON wire shape `{"events": [{seq, ts, kind, message, detail}, ...]}` is
    unchanged from the prior opaque-dict form; `model_dump(mode="json")` emits
    the same structure with `ts` serialized as an ISO-8601 string.
    """

    events: list[ActivityEvent] = []


class RunResult(BaseModel):
    """Result returned by `CodingAgentPlugin.parse_result`.

    Carries the structured skill response, optional error message, token usage,
    wall-clock duration, exit code, and the pre-rendered activity log.
    Does NOT carry status — the sink derives status from the wire event_kind
    and stores it on the run row independently of the plugin's parse step.

    `output` — the agent's structured response JSON (the `result` field from
    the terminal stream-json event). This is what `CodingAgentCommand.handle_response`
    validates against the command's `ExpectedResponse` schema.
    """

    output: str
    error_message: str | None = None
    usage: Usage
    duration_ms: int | None = None
    exit_code: int | None = None
    activity: ActivityLog


@runtime_checkable
class CodingAgentPlugin(Protocol):
    """Protocol every coding-agent plugin must satisfy.

    `plugin_id` identifies the plugin in the registry and on run rows.
    `compile_invocation` is a pure function that translates a high-level
    `Invocation` into a concrete `InvokeCodingAgent` exec block.
    `parse_result` is a pure function that decodes a terminal AgentEvent
    payload dict into a `RunResult`.
    `validate_settings` validates a raw settings dict and returns the
    normalized form; raises `ValueError` on invalid input.
    """

    plugin_id: str

    def compile_invocation(self, invocation: Invocation) -> InvokeCodingAgent:
        """Translate a high-level `Invocation` into a concrete exec block.

        Pure function — no IO, no session. The plugin owns skill resolution
        (mapping `invocation.skill` to an argv shape), model/effort mapping
        to vendor-specific CLI flags, and context encoding for the skill that
        runs inside the workspace. Raises `CodingAgentError` for unknown
        skills or missing configuration that can be verified without IO.
        """
        ...

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        """Decode a terminal AgentEvent payload into a `RunResult`.

        Pure function — no IO, no session. Reads `terminal_event_payload`
        (the `outputs` dict from the agent's terminal event) and returns a
        `RunResult` with `output`, `usage`, `activity`, `duration_ms`, and
        `exit_code` populated. Does NOT determine status — the sink sets
        `RunStatus` from the wire `event_kind`. Raises `CodingAgentError`
        on irrecoverable parse failure.
        """
        ...

    def validate_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Validate the raw settings dict and return the normalized dict.

        Pure function — no IO, no session. Raises `ValueError` on invalid
        input (unknown keys, bad types). The returned dict is the canonical
        form that gets persisted to the `org_coding_agents.settings` column.
        """
        ...

    def byok_requirement(self) -> str | None:
        """Return the BYOK provider_id this plugin needs, or None if stateless.

        Pure function — no IO. E.g. `ClaudeCodePlugin` returns `"anthropic"`;
        a plugin with credentials delivered via other means returns `None`.
        Used by `core/coding_agent.build_byok_secrets_for_org` to collect only
        the keys the registered plugins actually need.
        """
        ...


class CodingAgentError(Exception):
    """Infrastructure failure (subprocess won't spawn, config table unreadable)."""


class PluginNotFoundError(LookupError):
    """Plugin id not registered."""
