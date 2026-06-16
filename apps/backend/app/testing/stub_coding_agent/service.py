"""Wrapper plugin that fakes any `CodingAgentPlugin` for offline tests.

The bootstrap (when `YAAOS_CODING_AGENT_STUB` is set) walks the
`core/coding_agent` registry and replaces each registered plugin with a
`StubCodingAgentPlugin` wrapping it. From every consumer's perspective, nothing
changes — `dispatch_invocation` builds the exec block via `plugin.build_invocation`
and calls `plugin.parse_result` on terminal events; it just never touches a real
CLI or vendor API.

The stub returns canned success results for the two Protocol methods:
`build_invocation` and `parse_result`. It has zero knowledge of prompt content —
that's the real plugin's responsibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.coding_agent import (
    ActivityLog,
    Invocation,
    InvokeCodingAgent,
    RunResult,
    Usage,
)

log = structlog.get_logger("testing.stub_coding_agent")


def _canned_activity_log() -> ActivityLog:
    """Default activity log for the stub's `parse_result` path."""
    now = datetime.now(UTC).isoformat()
    return ActivityLog(
        events=[
            {
                "seq": 0,
                "ts": now,
                "kind": "session_start",
                "message": "Session started · model opus",
                "detail": {"model": "opus", "session_id": "stub-session"},
            },
            {
                "seq": 1,
                "ts": now,
                "kind": "subagent_dispatched",
                "message": "Dispatching yaaos-architecture",
                "detail": {"subagent": "yaaos-architecture"},
            },
            {
                "seq": 2,
                "ts": now,
                "kind": "tool_call_started",
                "message": "Read src/example.ts",
                "detail": {"tool": "Read", "input": {"file_path": "src/example.ts"}},
            },
            {"seq": 3, "ts": now, "kind": "result", "message": "Review complete", "detail": {"num_turns": 1}},
        ]
    )


_STUB_TOKENS_IN = 1000
_STUB_TOKENS_OUT = 200
_STUB_LATENCY_MS = 10


class StubCodingAgentPlugin:
    """Wraps a real `CodingAgentPlugin`; intercepts `build_invocation` and `parse_result`."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.plugin_id = wrapped.plugin_id

    def build_invocation(self, invocation: Invocation) -> InvokeCodingAgent:
        """Return a minimal stub exec block — argv=["stub"], empty env."""
        return InvokeCodingAgent(
            argv=["stub"],
            env={},
            stdin=None,
            wallclock_seconds=invocation.wallclock_seconds,
        )

    def validate_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Stub implementation — always succeeds, returns settings unchanged."""
        return dict(settings)

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        """Return a minimal stub `RunResult` from the payload."""
        stdout: str = terminal_event_payload.get("stdout", "") or ""
        exit_code_raw = terminal_event_payload.get("exit_code")
        exit_code: int | None = exit_code_raw if isinstance(exit_code_raw, int) else None
        return RunResult(
            output=stdout,
            error_message=None,
            usage=Usage(
                tokens_in=_STUB_TOKENS_IN,
                tokens_out=_STUB_TOKENS_OUT,
            ),
            duration_ms=_STUB_LATENCY_MS,
            exit_code=exit_code,
            activity=_canned_activity_log(),
        )


def wrap_all_registered_plugins() -> int:
    """Replace every registered coding-agent plugin with a stub wrapping it.

    Binds a fresh registry with wrapped entries; never mutates the canonical
    registry dict.
    """
    from app.core.coding_agent import (  # noqa: PLC0415
        CodingAgentRegistry,
        bind_coding_agent_registry,
        current_coding_agent_registry,
    )

    originals = current_coding_agent_registry().list()
    fresh = CodingAgentRegistry()
    count = 0
    for real in originals:
        if isinstance(real, StubCodingAgentPlugin):
            fresh.replace(real)
        else:
            fresh.replace(StubCodingAgentPlugin(wrapped=real))
            count += 1
    bind_coding_agent_registry(fresh)
    log.debug("stub_coding_agent.wrapped_all", count=count)
    return count
