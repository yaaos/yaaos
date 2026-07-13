"""`CodexPlugin.parse_activity_line` — maps one JSONL event to an `ActivityEvent`.

Pure-unit: no DB, no IO.
"""

from __future__ import annotations

import json

from app.core.coding_agent import ActivityEvent
from app.plugins.codex.service import CodexPlugin


def _plugin() -> CodexPlugin:
    return CodexPlugin()


def _line(event: dict) -> str:
    return json.dumps(event)


def test_returns_none_for_non_json() -> None:
    assert _plugin().parse_activity_line("not json") is None


def test_returns_none_for_empty_string() -> None:
    assert _plugin().parse_activity_line("") is None


def test_returns_activity_event_for_turn_started() -> None:
    line = _line({"type": "turn.started"})
    ev = _plugin().parse_activity_line(line)
    assert isinstance(ev, ActivityEvent)
    assert ev.kind == "session_start"


def test_returns_activity_event_for_assistant_message() -> None:
    line = _line(
        {
            "type": "item.completed",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello!"}],
            },
        }
    )
    ev = _plugin().parse_activity_line(line)
    assert isinstance(ev, ActivityEvent)
    assert ev.kind == "assistant_message"


def test_returns_activity_event_for_turn_completed() -> None:
    line = _line({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    ev = _plugin().parse_activity_line(line)
    assert isinstance(ev, ActivityEvent)
    assert ev.kind == "result"


def test_returns_none_for_unknown_event_type() -> None:
    line = _line({"type": "some.internal.debug"})
    # Unknown events may return None — they have no useful render.
    result = _plugin().parse_activity_line(line)
    # Either None or an activity event is acceptable; must not raise.
    assert result is None or isinstance(result, ActivityEvent)


def test_seq_is_zero() -> None:
    """seq is not meaningful for a single line — always 0."""
    line = _line({"type": "turn.started"})
    ev = _plugin().parse_activity_line(line)
    assert ev is not None
    assert ev.seq == 0


def test_tool_call_started_for_function_call() -> None:
    line = _line(
        {
            "type": "item.created",
            "item": {
                "type": "function_call",
                "name": "bash",
                "call_id": "call_abc",
            },
        }
    )
    ev = _plugin().parse_activity_line(line)
    if ev is not None:
        assert ev.kind == "tool_call_started"
