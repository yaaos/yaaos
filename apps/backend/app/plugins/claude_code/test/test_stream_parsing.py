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


# ── parse_usage ────────────────────────────────────────────────────────────────


def test_parse_usage_extracts_tokens_and_duration() -> None:
    """parse_usage reads input_tokens, output_tokens, and duration_ms from the
    terminal `type=result` event."""
    from app.plugins.claude_code.service import _plugin  # noqa: PLC0415

    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "s", "model": "m"},
        {
            "type": "result",
            "subtype": "success",
            "result": '{"findings": []}',
            "duration_ms": 1500,
            "usage": {"input_tokens": 1234, "output_tokens": 567},
        },
    )
    usage = _plugin.parse_usage(stream)
    assert usage.tokens_in == 1234
    assert usage.tokens_out == 567
    assert usage.duration_ms == 1500


def test_parse_usage_handles_missing_usage_block() -> None:
    """Missing `usage` block → tokens_in/out are None; duration_ms still read."""
    from app.plugins.claude_code.service import _plugin  # noqa: PLC0415

    stream = _stream(
        {"type": "result", "subtype": "success", "result": "{}", "duration_ms": 800},
    )
    usage = _plugin.parse_usage(stream)
    assert usage.tokens_in is None
    assert usage.tokens_out is None
    assert usage.duration_ms == 800


def test_parse_usage_no_terminal_event_returns_empty() -> None:
    """A stream with no terminal `result` event returns empty Usage()."""
    from app.plugins.claude_code.service import _plugin  # noqa: PLC0415

    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
    )
    usage = _plugin.parse_usage(stream)
    assert usage.tokens_in is None
    assert usage.tokens_out is None
    assert usage.duration_ms is None


def test_parse_usage_empty_stdout_returns_empty() -> None:
    """Empty stdout returns empty Usage() (no raise)."""
    from app.plugins.claude_code.service import _plugin  # noqa: PLC0415

    usage = _plugin.parse_usage("")
    assert usage.tokens_in is None
    assert usage.tokens_out is None
    assert usage.duration_ms is None


# ── render_activity ────────────────────────────────────────────────────────────


def test_render_activity_walks_full_stream_with_monotonic_seq() -> None:
    """render_activity produces one ActivityEvent per useful stream line, with
    monotonic seq starting at 0."""
    from app.plugins.claude_code.service import _plugin  # noqa: PLC0415

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
    log = _plugin.render_activity(stream)
    assert len(log.events) == 4
    assert [ev.seq for ev in log.events] == [0, 1, 2, 3]
    assert [ev.kind for ev in log.events] == [
        "session_start",
        "tool_call_started",
        "tool_call_finished",
        "result",
    ]


def test_render_activity_skips_null_renders() -> None:
    """Events with no useful render are dropped; remaining seq stays monotonic."""
    from app.plugins.claude_code.service import _plugin  # noqa: PLC0415

    stream = _stream(
        {"type": "system", "subtype": "init", "session_id": "s", "model": "opus"},
        {"type": "unknown_kind"},  # filtered → no ActivityEvent
        {"type": "result", "subtype": "success", "result": "{}"},
    )
    log = _plugin.render_activity(stream)
    assert len(log.events) == 2
    assert [ev.seq for ev in log.events] == [0, 1]


def test_render_activity_empty_stdout_returns_empty() -> None:
    """Empty stdout returns an empty ActivityLog."""
    from app.plugins.claude_code.service import _plugin  # noqa: PLC0415

    log = _plugin.render_activity("")
    assert log.events == ()
