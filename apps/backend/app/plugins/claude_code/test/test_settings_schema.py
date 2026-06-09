"""Settings round-trip for `{mcp_proxy_ids}` shape after orchestrator retirement."""

from __future__ import annotations

import pytest

from app.plugins.claude_code.settings_schema import validate_settings


def test_empty_dict_validates_to_empty_mcp_proxy_ids() -> None:
    out = validate_settings({})
    assert out["mcp_proxy_ids"] == []


def test_mcp_proxy_ids_accepts_uuid_list() -> None:
    out = validate_settings({"mcp_proxy_ids": ["00000000-0000-0000-0000-000000000001"]})
    assert len(out["mcp_proxy_ids"]) == 1
    # Pydantic coerces strings to UUIDs.
    assert str(out["mcp_proxy_ids"][0]) == "00000000-0000-0000-0000-000000000001"


def test_mcp_proxy_ids_multiple_uuids() -> None:
    ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]
    out = validate_settings({"mcp_proxy_ids": ids})
    assert len(out["mcp_proxy_ids"]) == 2


def test_unknown_top_level_keys_rejected() -> None:
    with pytest.raises(ValueError):
        validate_settings({"mcp_proxy_ids": [], "rogue": True})


def test_invalid_uuid_rejected() -> None:
    with pytest.raises(ValueError):
        validate_settings({"mcp_proxy_ids": ["not-a-uuid"]})
