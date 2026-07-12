"""`CodexPlugin.parse_result` — decodes a terminal AgentEvent payload into `RunResult`.

Pure-unit: no DB, no IO.
"""

from __future__ import annotations

import json

from app.core.coding_agent import RunResult
from app.plugins.codex.service import CodexPlugin


def _plugin() -> CodexPlugin:
    return CodexPlugin()


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _payload(stdout: str = "", exit_code: int | None = 0) -> dict:
    return {"stdout": stdout, "exit_code": exit_code}


def test_returns_run_result_instance() -> None:
    result = _plugin().parse_result(_payload())
    assert isinstance(result, RunResult)


def test_output_extracted_from_item_completed_assistant_text() -> None:
    """output is the last assistant-message text from item.completed events."""
    text = '{"findings": []}'
    stdout = _jsonl(
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.output == text


def test_output_empty_when_no_assistant_message() -> None:
    """When stdout has no item.completed assistant event, output is empty string."""
    stdout = _jsonl({"type": "turn.started"})
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.output == ""


def test_exit_code_propagated() -> None:
    result = _plugin().parse_result(_payload(exit_code=1))
    assert result.exit_code == 1


def test_exit_code_none_when_missing() -> None:
    result = _plugin().parse_result({"stdout": ""})
    assert result.exit_code is None


def test_usage_extracted_from_turn_completed() -> None:
    stdout = _jsonl(
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 200}},
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.usage.tokens_in == 100
    assert result.usage.tokens_out == 200


def test_usage_none_when_missing() -> None:
    result = _plugin().parse_result(_payload())
    assert result.usage.tokens_in is None
    assert result.usage.tokens_out is None


def test_multiple_content_parts_concatenated() -> None:
    """Multiple output_text parts in a single assistant message are joined."""
    stdout = _jsonl(
        {
            "type": "item.completed",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "hello "},
                    {"type": "output_text", "text": "world"},
                ],
            },
        },
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert result.output == "hello world"


def test_activity_log_has_events_for_jsonl() -> None:
    stdout = _jsonl(
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}},
    )
    result = _plugin().parse_result(_payload(stdout=stdout))
    assert len(result.activity.events) > 0


def test_malformed_stdout_does_not_raise() -> None:
    """Non-JSON stdout produces a zero-usage RunResult without raising."""
    result = _plugin().parse_result(_payload(stdout="not json\nalso not json\n"))
    assert isinstance(result, RunResult)
    assert result.output == ""


def test_empty_stdout_returns_zero_result() -> None:
    result = _plugin().parse_result(_payload(stdout=""))
    assert result.output == ""
    assert result.usage.tokens_in is None
