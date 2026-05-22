"""Trust-boundary audit: ActivityEvent pre-renderer ships metadata only.

ActivityEvents cross from inside the customer's workspace (where the
Claude Code CLI runs against their source) to yaaos' control plane (DB
+ SSE feed to the SPA). The audit rule: no source content may cross.
Only metadata — tool names, file paths, sizes, durations — is allowed.

These tests assert the `_render_activity` pre-renderer enforces that
rule for the highest-risk surfaces:

1. Content-bearing tool inputs (Edit, Write, MultiEdit, NotebookEdit) —
   `input.new_string` / `input.content` MUST NOT leak into the event.
2. Tool results (Read body, Bash stdout, Grep matches) — the result
   excerpt MUST be replaced with a size-only summary.
3. Bash commands — capped at 120 chars, no expansion.

Failing this audit means a yaaos operator with DB or SSE access could
read customer source. That violates the architecture's core trust
boundary (see docs/system-security.md § Source code stays in-VPC).
"""

from __future__ import annotations

import json

from app.plugins.claude_code.service import _render_activity, _safe_tool_input


def test_edit_tool_input_does_not_leak_new_string() -> None:
    """Edit tool's `new_string` is the source content the agent is about
    to commit. It MUST be stripped from the activity event."""
    leaked_source = "API_KEY = 'sk-leaked-secret-do-not-ship'\ndef foo(): return 42"
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "Edit",
                    "input": {
                        "file_path": "src/auth.py",
                        "old_string": "OLD",
                        "new_string": leaked_source,
                    },
                }
            ]
        },
    }
    activity = _render_activity(event)
    assert activity is not None
    serialized = json.dumps(activity.model_dump(mode="json"))
    assert "sk-leaked-secret" not in serialized
    assert "API_KEY" not in serialized
    assert "src/auth.py" in serialized  # path metadata is OK


def test_write_tool_input_does_not_leak_content() -> None:
    """Write tool's `content` is the full file body the agent is creating."""
    secret_content = "password = 'hunter2'\n" * 50
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_2",
                    "name": "Write",
                    "input": {"file_path": "config.py", "content": secret_content},
                }
            ]
        },
    }
    activity = _render_activity(event)
    assert activity is not None
    serialized = json.dumps(activity.model_dump(mode="json"))
    assert "hunter2" not in serialized


def test_multiedit_tool_input_does_not_leak_replacements() -> None:
    """MultiEdit's `edits` array carries multiple new_string fields."""
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_3",
                    "name": "MultiEdit",
                    "input": {
                        "file_path": "src/foo.py",
                        "edits": [
                            {"old_string": "x", "new_string": "AWS_SECRET_HERE"},
                            {"old_string": "y", "new_string": "PRIVATE_KEY_BLOB"},
                        ],
                    },
                }
            ]
        },
    }
    activity = _render_activity(event)
    assert activity is not None
    serialized = json.dumps(activity.model_dump(mode="json"))
    assert "AWS_SECRET" not in serialized
    assert "PRIVATE_KEY" not in serialized


def test_tool_result_does_not_leak_read_body() -> None:
    """A Read tool_result contains the file body. The activity event must
    report size only — not the body."""
    file_body = "import secrets\nDB_PASSWORD = 'production-db-pw-do-not-ship'\n"
    event = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "is_error": False,
                    "content": file_body,
                }
            ]
        },
    }
    activity = _render_activity(event)
    assert activity is not None
    serialized = json.dumps(activity.model_dump(mode="json"))
    assert "DB_PASSWORD" not in serialized
    assert "do-not-ship" not in serialized
    # Size metadata is fine.
    assert str(len(file_body)) in serialized


def test_tool_result_error_does_not_leak_error_body() -> None:
    """Even on `is_error=True` the message field reveals only the error
    flag — the error body itself stays inside the workspace."""
    error_body = "Traceback (most recent call last):\n  File '/Users/customer/src/x.py'\n…"
    event = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "is_error": True,
                    "content": error_body,
                }
            ]
        },
    }
    activity = _render_activity(event)
    assert activity is not None
    serialized = json.dumps(activity.model_dump(mode="json"))
    assert "customer" not in serialized
    assert "Traceback" not in serialized
    assert activity.detail.get("is_error") is True


def test_bash_tool_command_caps_at_120_chars() -> None:
    """Bash commands could embed source paths or values; the pre-renderer
    caps them at 120 chars so long commands can't smuggle bulk content."""
    long_cmd = "echo " + ("x" * 500)
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_4",
                    "name": "Bash",
                    "input": {"command": long_cmd},
                }
            ]
        },
    }
    activity = _render_activity(event)
    assert activity is not None
    prefix = activity.detail["input_summary"]["command_prefix"]
    assert len(prefix) <= 120
    assert prefix.endswith("…")


def test_safe_tool_input_drops_unknown_tools() -> None:
    """An unknown tool (future addition, or attacker-crafted tool_use)
    yields an empty dict — fail-closed default."""
    assert _safe_tool_input("UnknownNewTool", {"secret": "leak", "data": "leak"}) == {}


def test_read_tool_keeps_only_path() -> None:
    """Read tool input has only `file_path`; no payload to leak."""
    safe = _safe_tool_input("Read", {"file_path": "src/x.py", "limit": 100, "offset": 0})
    assert "limit" not in safe
    assert safe == {"target": "src/x.py"}


def test_grep_tool_keeps_pattern_and_path() -> None:
    """Grep input is two metadata strings; both are kept."""
    safe = _safe_tool_input("Grep", {"pattern": "TODO", "path": "src/"})
    assert safe == {"pattern": "TODO", "path": "src/"}
