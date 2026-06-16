"""Types + Protocol for the coding-agent abstraction.

`CodingAgentPlugin` is the two-method Protocol every coding-agent plugin
must satisfy: `build_invocation` translates a high-level `Invocation` into
a concrete `InvokeCodingAgent` exec block; `parse_result` decodes a terminal
AgentEvent payload into a `RunResult`. Plugins own skill resolution, model
mapping, and stdout parsing; `core/coding_agent` owns dispatch and the run
lifecycle.

`ReviewContext` and `ReportedFinding` live in `domain/reviewer` — they are
reviewer-domain types, not generic coding-agent types.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

# Plugin/model-specific effort level string. The plugin validates its
# own allowed values; `core/coding_agent` treats this as an opaque str.
Effort = str


class Invocation(BaseModel):
    """High-level intent passed to `CodingAgentPlugin.build_invocation`.

    Carries the skill name, model, effort level, generic context dict
    (plugin interprets the keys for the skill), and the wallclock cap.
    Pure data — no exec block. `build_invocation` translates this into
    an `InvokeCodingAgent` with the concrete argv/env/stdin the agent runs.
    """

    skill: str
    model: str
    effort: Effort
    context: Mapping[str, Any]
    wallclock_seconds: int


class InvokeCodingAgent(BaseModel):
    """Concrete exec block returned by `CodingAgentPlugin.build_invocation`.

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


class ActivityLog(BaseModel):
    """Pre-rendered activity stream for one coding-agent run.

    Produced by `CodingAgentPlugin.parse_result` — the same event sequence
    the in-process path streams via `OnActivity`, captured durably for the
    Activity tab. Persisted as a JSONB blob in the partitioned
    `coding_agent_activity` table.

    Element type is an opaque `Mapping[str, Any]` — the model no longer
    enforces inner schema. JSON wire shape `{"events": [...]}` is unchanged.
    """

    events: list[Mapping[str, Any]] = []


class RunResult(BaseModel):
    """Result returned by `CodingAgentPlugin.parse_result`.

    Carries the raw skill stdout, optional error message, token usage,
    wall-clock duration, exit code, and the pre-rendered activity log.
    Does NOT carry status — the sink derives status from the wire event_kind
    and stores it on the run row independently of the plugin's parse step.
    """

    output: str
    error_message: str | None = None
    usage: Usage
    duration_ms: int | None = None
    exit_code: int | None = None
    activity: ActivityLog


@runtime_checkable
class CodingAgentPlugin(Protocol):
    """Two-method Protocol every coding-agent plugin must satisfy.

    `plugin_id` identifies the plugin in the registry and on run rows.
    `build_invocation` is a pure function that translates a high-level
    `Invocation` into a concrete `InvokeCodingAgent` exec block.
    `parse_result` is a pure function that decodes a terminal AgentEvent
    payload dict into a `RunResult`.
    """

    plugin_id: str

    def build_invocation(self, invocation: Invocation) -> InvokeCodingAgent:
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


class CodingAgentError(Exception):
    """Infrastructure failure (subprocess won't spawn, config table unreadable)."""


class PluginNotFoundError(LookupError):
    """Plugin id not registered."""
