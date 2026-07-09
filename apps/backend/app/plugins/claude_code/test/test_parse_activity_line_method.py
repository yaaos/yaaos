"""`ClaudeCodePlugin.parse_activity_line` — maps one stream-json line to a
renderable `ActivityEvent`. Pure-unit: no DB, no IO.

Cases mirror `_render_activity`'s taxonomy (the shared internal the method
factors through) so live and persisted views never diverge.
"""

from __future__ import annotations

import json

from app.core.coding_agent import ActivityEvent
from app.plugins.claude_code.service import ClaudeCodePlugin


def _plugin() -> ClaudeCodePlugin:
    return ClaudeCodePlugin()


def _line(event: dict) -> str:
    return json.dumps(event)


def test_tool_use_line_renders_tool_call_started() -> None:
    line = _line(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}}]
            },
        }
    )
    result = _plugin().parse_activity_line(line)
    assert isinstance(result, ActivityEvent)
    assert result.kind == "tool_call_started"
    assert "Read" in result.message


def test_task_tool_use_renders_subagent_dispatched() -> None:
    line = _line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Task",
                        "input": {"subagent_type": "yaaos-architecture"},
                    }
                ]
            },
        }
    )
    result = _plugin().parse_activity_line(line)
    assert result is not None
    assert result.kind == "subagent_dispatched"
    assert result.detail["subagent"] == "yaaos-architecture"


def test_assistant_text_renders_assistant_message() -> None:
    line = _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi there"}]}})
    result = _plugin().parse_activity_line(line)
    assert result is not None
    assert result.kind == "assistant_message"
    assert result.message == "Hi there"


def test_system_init_renders_session_start() -> None:
    line = _line({"type": "system", "subtype": "init", "model": "opus", "session_id": "s1"})
    result = _plugin().parse_activity_line(line)
    assert result is not None
    assert result.kind == "session_start"


def test_result_event_renders_result() -> None:
    line = _line({"type": "result", "subtype": "success", "duration_ms": 10, "num_turns": 1})
    result = _plugin().parse_activity_line(line)
    assert result is not None
    assert result.kind == "result"


def test_unrenderable_type_returns_none() -> None:
    """An unknown event type has no useful render — None, not an error."""
    line = _line({"type": "unknown_type"})
    assert _plugin().parse_activity_line(line) is None


def test_malformed_json_returns_none() -> None:
    assert _plugin().parse_activity_line("not json at all") is None


def test_blank_line_returns_none() -> None:
    assert _plugin().parse_activity_line("") is None


def test_non_dict_json_returns_none() -> None:
    """A parseable-but-non-object JSON line (e.g. a bare list) renders nothing."""
    assert _plugin().parse_activity_line("[1, 2, 3]") is None


def test_seq_is_always_zero() -> None:
    """A single line carries no run-wide ordering context."""
    line = _line({"type": "system", "subtype": "init", "model": "opus"})
    result = _plugin().parse_activity_line(line)
    assert result is not None
    assert result.seq == 0
