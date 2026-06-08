"""Pydantic settings model for the `claude_code` coding-agent plugin.

The settings JSONB shape is:

    {mcp_proxy_ids?: [UUID]}

`extra="forbid"` — old orchestrator/agents rows won't reparse (clean
cutover, pre-prod). Persisted via `org_coding_agents.settings` (JSONB).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ClaudeCodeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # MCP-proxy connections to expose as context for this org's runs.
    # Optional with empty-default so rows without it still parse.
    mcp_proxy_ids: list[UUID] = Field(default_factory=list)


def validate_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Public validator used by `ClaudeCodePlugin.validate_settings`. Returns
    a normalized dict; raises `ValueError` (with Pydantic detail attached
    via `__cause__`) on invalid input."""
    try:
        parsed = ClaudeCodeSettings.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError subclasses Exception
        raise ValueError(str(exc)) from exc
    return parsed.model_dump(mode="python")
