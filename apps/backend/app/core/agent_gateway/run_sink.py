"""IoC seam for coding-agent run lifecycle.

`AgentRunSink` is the Protocol that `domain/coding_agent` implements and
registers here at import time. `core/agent_gateway` calls the registered
sink on every terminal AgentEvent without importing `domain/coding_agent`.

Canonical import direction:
  domain/coding_agent → core/agent_gateway  (registers here)
  core/agent_gateway  → core/agent_gateway.run_sink  (calls it)

The sink fires only on `InvokeClaudeCode` terminal events; all other
command kinds are silently no-ops at the implementation level.
"""

from __future__ import annotations

from typing import Protocol, TypedDict
from uuid import UUID


class AgentEventEnrichment(TypedDict, total=False):
    """Typed return value from `AgentRunSink.handle_terminal_event`.

    Keys:
    - `output` — the structured skill response JSON extracted by `plugin.parse_result`
      from the stream-json `result` field; forwarded to the run as `outputs["output"]`
      and validated by `CodingAgentCommand.handle_response` against `ExpectedResponse`.
    - `error_message` — structured error text from the run; `None` when the
      agent completed without a reported error.
    """

    output: str
    error_message: str | None


class AgentRunSink(Protocol):
    """Contract that `domain/coding_agent` implements and registers.

    `core/agent_gateway` calls `handle_terminal_event` on every terminal
    AgentEvent. Implementations filter on `command_kind` and no-op for
    unrecognised kinds.
    """

    async def handle_terminal_event(
        self,
        command_id: UUID,
        command_kind: str,
        event_kind: str,
        outputs: dict,  # type: ignore[type-arg]
        session: object,  # AsyncSession
    ) -> AgentEventEnrichment | None:
        """Handle a terminal AgentEvent.

        `command_kind` is `agent_commands.command_kind` (e.g. `"InvokeClaudeCode"`).
        `event_kind` is `completed_success` | `completed_failure` | `completed_skipped`.
        `outputs` is `AgentEvent.outputs` — for `InvokeClaudeCode` carries
        `exit_code` (int) and `stdout` (str).
        `session` is an `AsyncSession`; caller commits.

        Returns an `AgentEventEnrichment` whose keys are merged into `outputs`
        before the `HANDLE_AGENT_EVENT` task is enqueued — sink keys override
        same-key native values. Canonical keys: `output` (parsed skill stdout
        that replaces raw `stdout` downstream) and `error_message` (structured
        error text; `None` when none). Return `None` to leave `outputs`
        unchanged.
        """
        ...


# ── Single-slot registry ───────────────────────────────────────────────

_SINK: AgentRunSink | None = None


def register_run_sink(sink: AgentRunSink) -> None:
    """Register the module-global run sink.

    Idempotent for the same instance; raises `RuntimeError` on conflicting
    re-registration so a double-wiring bug surfaces at boot.
    Tests swap stubs via `clear_run_sink`.
    """
    global _SINK
    if _SINK is not None and _SINK is not sink:
        raise RuntimeError("AgentRunSink already registered — clear it before re-registering")
    _SINK = sink


def get_run_sink() -> AgentRunSink | None:
    """Return the registered sink, or None when not yet registered.

    In production, web.py and worker.py both assert the return value is not
    None after `core/coding_agent` is imported. The None return is only
    reachable in tests that explicitly clear the slot via `clear_run_sink`.
    """
    return _SINK


def clear_run_sink() -> None:
    """Reset the registry slot. Used in tests to swap stub sinks."""
    global _SINK
    _SINK = None
