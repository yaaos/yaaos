"""ActivityEvent typed construction — producer emits typed events.

Checks that `_render_activity_log` returns `ActivityEvent` instances (not raw
dicts) and that each has a proper `datetime` timestamp. Pure-unit; no DB, no IO.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.core.coding_agent import ACTIVITY_EVENT_KINDS, ActivityEvent
from app.plugins.claude_code.service import _render_activity_log


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_render_activity_log_returns_activity_event_instances() -> None:
    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "s", "model": "opus"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Read",
                        "input": {"file_path": "a.py"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "hi"}]},
        },
        {"type": "result", "subtype": "success", "result": "{}", "duration_ms": 100},
    )
    log = _render_activity_log(stream)
    assert len(log.events) == 4
    for ev in log.events:
        assert isinstance(ev, ActivityEvent), f"Expected ActivityEvent, got {type(ev)}"
        assert isinstance(ev.ts, datetime), f"Expected datetime for ts, got {type(ev.ts)}"


def test_render_activity_log_seq_is_monotonic() -> None:
    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "s", "model": "opus"},
        {"type": "result", "subtype": "success", "result": "{}"},
    )
    log = _render_activity_log(stream)
    assert [ev.seq for ev in log.events] == [0, 1]


def test_render_activity_log_kinds_are_valid() -> None:
    """Every kind the producer emits must be in the canonical Literal set."""
    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "s", "model": "opus"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu2",
                        "name": "Task",
                        "input": {"subagent_type": "yaaos-architecture", "description": "review"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu3",
                        "name": "Bash",
                        "input": {"command": "git diff"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Thinking..."}]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "tu3", "content": "diff output"}]},
        },
        {"type": "result", "subtype": "success", "result": "{}"},
    )
    log = _render_activity_log(stream)
    for ev in log.events:
        assert ev.kind in ACTIVITY_EVENT_KINDS, f"Kind {ev.kind!r} not in canonical set"
