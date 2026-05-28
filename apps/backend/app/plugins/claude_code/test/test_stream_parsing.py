"""Stream-json parsing — pure helpers used by `_run_and_parse_envelope`.

The CLI emits one JSON object per line under `--output-format=stream-json`.
We parse the captured stdout post-hoc to extract the terminal `result` event
and log a per-event trace. These tests exercise the parser against canned
streams; live CLI streaming is covered by manual review and e2e.
"""

from __future__ import annotations

import json

import pytest

from app.plugins.claude_code.service import _log_stream_event, _parse_stream_events


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_parses_well_formed_stream() -> None:
    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "abc", "model": "claude-sonnet-4"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Starting…"}]}},
        {"type": "result", "subtype": "success", "result": '{"findings":[]}', "usage": {"input_tokens": 5}},
    )
    events = _parse_stream_events(stream)
    assert len(events) == 3
    assert events[-1]["type"] == "result"


def test_skips_blank_lines_and_garbage() -> None:
    stream = (
        '{"type":"system","subtype":"init"}\n'
        "\n"
        "not json at all\n"
        "  \n"
        '{"type":"result","subtype":"success","result":"{}"}\n'
    )
    events = _parse_stream_events(stream)
    assert [e["type"] for e in events] == ["system", "result"]


def test_returns_empty_for_empty_stdout() -> None:
    assert _parse_stream_events("") == []


def test_partial_stream_on_timeout() -> None:
    """Simulates a timed-out run: system + a few assistant turns, no `result`."""
    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "abc"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Task",
                        "input": {"subagent_type": "yaaos-architecture", "prompt": "review"},
                    }
                ]
            },
        },
    )
    events = _parse_stream_events(stream)
    assert len(events) == 2
    # The diagnostic for a stuck run: the last tool_use shows which subagent
    # was in flight when time ran out.
    tool_use = events[-1]["message"]["content"][0]
    assert tool_use["input"]["subagent_type"] == "yaaos-architecture"


def test_log_event_handles_each_type_without_raising(caplog: pytest.LogCaptureFixture) -> None:
    """Smoke test — the logger must not raise on any well-formed event shape."""
    for ev in [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "m"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "x",
                        "name": "Task",
                        "input": {"subagent_type": "yaaos-security"},
                    },
                    {"type": "text", "text": "thinking"},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok", "is_error": False}]
            },
        },
        {"type": "result", "subtype": "success", "duration_ms": 1234, "num_turns": 4},
        {"type": "unknown"},  # forward-compatible: skip silently
    ]:
        _log_stream_event(ev)


def test_log_event_handles_missing_fields_gracefully() -> None:
    """Robustness: stream events from a future CLI version may add/drop keys."""
    _log_stream_event({"type": "assistant"})
    _log_stream_event({"type": "assistant", "message": {}})
    _log_stream_event({"type": "assistant", "message": {"content": None}})
    _log_stream_event({"type": "user", "message": {"content": [{"type": "tool_result"}]}})
