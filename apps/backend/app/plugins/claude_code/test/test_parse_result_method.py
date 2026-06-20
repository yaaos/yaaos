"""`ClaudeCodePlugin.parse_result` — decodes a terminal AgentEvent payload into `RunResult`.

Pure-unit: no DB, no IO.
"""

from __future__ import annotations

import json

from app.core.coding_agent import RunResult
from app.plugins.claude_code.service import ClaudeCodePlugin


def _plugin() -> ClaudeCodePlugin:
    return ClaudeCodePlugin()


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _payload(stdout: str = "", exit_code: int | None = 0) -> dict:
    return {"stdout": stdout, "exit_code": exit_code}


def test_returns_run_result_instance() -> None:
    result = _plugin().parse_result(_payload())
    assert isinstance(result, RunResult)


def test_output_is_extracted_result_json() -> None:
    """output is the `result` field from the terminal stream event, not the full stdout.

    This is the structured response JSON that `CodingAgentCommand.handle_response`
    validates against the command's `ExpectedResponse` schema.
    """
    findings_json = '{"findings": []}'
    stdout = _stream(
        {"type": "system", "subtype": "init", "session_id": "s"},
        {
            "type": "result",
            "subtype": "success",
            "result": findings_json,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "duration_ms": 100,
        },
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.output == findings_json


def test_output_empty_when_no_result_event() -> None:
    """When stdout has no `type=result` event (e.g. timeout), output is empty string."""
    stdout = _stream({"type": "system", "subtype": "init", "session_id": "s"})
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.output == ""


def test_exit_code_propagated() -> None:
    result = _plugin().parse_result(_payload(exit_code=1))
    assert result.exit_code == 1


def test_exit_code_none_when_absent() -> None:
    result = _plugin().parse_result({"stdout": ""})
    assert result.exit_code is None


def test_error_message_is_none() -> None:
    """parse_result never sets error_message — the sink derives that from the
    wire event_kind (completed_success vs completed_failure)."""
    result = _plugin().parse_result(_payload())
    assert result.error_message is None


def test_usage_tokens_parsed_from_stdout() -> None:
    stdout = _stream(
        {"type": "system", "subtype": "init", "session_id": "s"},
        {
            "type": "result",
            "subtype": "success",
            "result": "{}",
            "usage": {"input_tokens": 42, "output_tokens": 7},
            "duration_ms": 500,
        },
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.usage.tokens_in == 42
    assert result.usage.tokens_out == 7
    # duration_ms is on RunResult, not on Usage
    assert result.duration_ms == 500


def test_usage_empty_when_no_result_event() -> None:
    """A timed-out run has no `type=result` event; usage should be empty, not raise."""
    stdout = _stream({"type": "system", "subtype": "init", "session_id": "s"})
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.usage.tokens_in is None
    assert result.usage.tokens_out is None


def test_duration_ms_on_run_result() -> None:
    """duration_ms lives on RunResult, not on Usage."""
    stdout = _stream(
        {
            "type": "result",
            "subtype": "success",
            "result": "{}",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "duration_ms": 999,
        }
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.duration_ms == 999
    # Usage is tokens-only — no duration_ms attribute
    assert not hasattr(result.usage, "duration_ms")


def test_activity_log_non_empty_for_real_stream() -> None:
    """A stream with parseable events produces a non-empty ActivityLog."""
    stdout = _stream(
        {"type": "system", "subtype": "init", "session_id": "s", "model": "opus"},
        {
            "type": "result",
            "subtype": "success",
            "result": "{}",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "duration_ms": 10,
        },
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    # At least the session_start and result events should render.
    assert len(result.activity.events) > 0


def test_missing_stdout_key_treated_as_empty() -> None:
    """Payload with no 'stdout' key must not raise."""
    result = _plugin().parse_result({"exit_code": 0})
    assert result.output == ""


def test_non_int_exit_code_becomes_none() -> None:
    result = _plugin().parse_result({"stdout": "", "exit_code": "0"})
    assert result.exit_code is None
