"""`_materialize_mcp_config` writes `.mcp.json` + returns allowed-tools extras."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.plugins.claude_code.service import _materialize_mcp_config


class _FakeWorkspace:
    """Minimal Workspace shim — captures written files in-memory."""

    def __init__(self) -> None:
        self.id = "fake"
        self.written: dict[str, str] = {}

    async def write_text(self, path: str, content: str) -> None:
        if path in self.written:
            raise RuntimeError(f"already exists: {path}")
        self.written[path] = content


@pytest.mark.asyncio
async def test_returns_empty_when_no_payload() -> None:
    ws = _FakeWorkspace()
    extras = await _materialize_mcp_config(ws, None)
    assert extras == []
    assert ws.written == {}


@pytest.mark.asyncio
async def test_writes_mcp_json_and_returns_namespaced_tools() -> None:
    ws = _FakeWorkspace()
    payload: dict[str, Any] = {
        "token": "raw-bearer-xyz",
        "base_url": "http://app.test/api/mcp/abc",
        "servers": [
            {
                "provider": "linear",
                "allowed_tools": ["update_issue"],
                "known_read_tools": ["get_issue", "search_issues"],
                "known_write_tools": ["update_issue", "create_comment"],
            },
            {
                "provider": "notion",
                "allowed_tools": [],
                "known_read_tools": ["search"],
                "known_write_tools": ["update_page"],
            },
        ],
    }
    extras = await _materialize_mcp_config(ws, payload)

    cfg = json.loads(ws.written[".mcp.json"])
    assert set(cfg["mcpServers"].keys()) == {"linear", "notion"}
    linear = cfg["mcpServers"]["linear"]
    assert linear["type"] == "http"
    assert linear["url"] == "http://app.test/api/mcp/abc/linear"
    assert linear["headers"]["Authorization"] == "Bearer raw-bearer-xyz"

    # Read tools always allowed; write tools only when in row's allowed_tools.
    assert "mcp__linear__get_issue" in extras
    assert "mcp__linear__update_issue" in extras
    assert "mcp__linear__create_comment" not in extras
    assert "mcp__notion__search" in extras
    assert "mcp__notion__update_page" not in extras
