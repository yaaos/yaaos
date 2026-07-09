"""Standalone fake `CodingAgentPlugin` for tests that don't have a real
plugin registered (the existing `stub_coding_agent` wraps a real plugin;
this one stands alone).

Each method returns a deterministic, schema-valid result so command-body
tests can drive a review end-to-end without real plugin auth.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.core.coding_agent import (
    ActivityEvent,
    ActivityLog,
    Invocation,
    InvokeCodingAgent,
    RunResult,
    Usage,
)

_TOKENS_IN = 0
_TOKENS_OUT = 0


class FakeCodingAgentPlugin:
    """Minimal `CodingAgentPlugin` impl satisfying the Protocol.

    Tests can override the canned returns by mutating the public attributes
    on the registered instance.
    """

    def __init__(self, plugin_id: str = "claude_code") -> None:
        self.plugin_id = plugin_id
        # Overridable per-instance return values.
        self.compile_invocation_result: InvokeCodingAgent | None = None

    def compile_invocation(self, invocation: Invocation) -> InvokeCodingAgent:
        """Return a stable canned exec block for the given invocation."""
        if self.compile_invocation_result is not None:
            return self.compile_invocation_result
        return InvokeCodingAgent(
            argv=["fake-claude", "--skill", invocation.skill, "--model", invocation.model],
            env={},
            stdin=None,
            wallclock_seconds=invocation.wallclock_seconds,
        )

    def parse_result(self, terminal_event_payload: Mapping[str, Any]) -> RunResult:
        """Return a minimal canned `RunResult`."""
        stdout: str = terminal_event_payload.get("stdout", "") or ""
        exit_code_raw = terminal_event_payload.get("exit_code")
        exit_code: int | None = exit_code_raw if isinstance(exit_code_raw, int) else None
        return RunResult(
            output=stdout,
            error_message=None,
            usage=Usage(tokens_in=_TOKENS_IN, tokens_out=_TOKENS_OUT),
            duration_ms=0,
            exit_code=exit_code,
            activity=ActivityLog(events=[]),
        )

    def validate_settings(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Fake implementation — always succeeds, returns settings unchanged."""
        return dict(settings)

    def api_key_requirement(self) -> str | None:
        """Fake implementation — stateless, needs no API key."""
        return None

    def parse_activity_line(self, line: str) -> ActivityEvent | None:
        """Deterministic fake mapping: every non-blank line renders as an
        `assistant_message` event; blank lines render nothing."""
        text = line.strip()
        if not text:
            return None
        return ActivityEvent(
            seq=0,
            ts=datetime.now(UTC),
            kind="assistant_message",
            message=text,
            detail={},
        )


@contextmanager
def register_fake_coding_agent(plugin_id: str = "claude_code"):  # type: ignore[no-untyped-def]
    """Context manager: register a `FakeCodingAgentPlugin` under `plugin_id`,
    yielding the instance for setup + assertions. Restores prior registry
    binding on exit.

    Binds a fresh registry copy with the fake substituted; restores the prior
    binding on exit. Never mutates the canonical registry dict.
    """
    from app.core.coding_agent import set_coding_agents_for_tests  # noqa: PLC0415

    fake = FakeCodingAgentPlugin(plugin_id=plugin_id)
    with set_coding_agents_for_tests() as reg:
        reg.replace(fake)  # type: ignore[arg-type]
        yield fake


__all__ = ["FakeCodingAgentPlugin", "register_fake_coding_agent"]
