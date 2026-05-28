"""Pydantic settings model for the `claude_code` coding-agent plugin.

Lives in the plugin (not `domain/orgs`) because the schema is intimately
tied to the plugin's runtime contract. `domain/orgs.install_coding_agent`
calls `validate_settings()` via the plugin's `Plugin.validate_settings`
hook, which delegates here. The settings JSONB shape is:

    {orchestrator: AgentSettings, agents: [AgentSettings, ...], mcp_proxy_ids?: [UUID]}

Constraints:
- Sub-agent count: 1 ≤ len(agents) ≤ 8.
- Sub-agent names: unique within `agents`, length 1..64.
- model, version, effort: must be from the enum lists in `defaults.py`.

additions (all optional, default-friendly so older DB rows
continue to parse without migration):
- `AgentSettings.use_default_system_prompt: bool = True` — when true the
  plugin uses its built-in system prompt and the per-agent `system_prompt`
  is ignored.
- `AgentSettings.system_prompt: str | None = None` — overridden text;
  consumed only when `use_default_system_prompt is False`.
- `ClaudeCodeSettings.mcp_proxy_ids: list[UUID] = []` — references to
  configured `domain/integrations` MCP proxy connections that the
  orchestrator should expose as MCP context for this org's runs.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.plugins.claude_code.defaults import EFFORTS, MODELS, VERSIONS


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1)
    model: str
    version: str
    effort: str
    updated_at: str = ""
    # system-prompt overrides per E2a.2. Optional so existing
    # rows in `org_coding_agents.settings` keep parsing.
    use_default_system_prompt: bool = True
    system_prompt: str | None = None

    @field_validator("model")
    @classmethod
    def _model_in_enum(cls, v: str) -> str:
        if v not in MODELS:
            raise ValueError(f"model must be one of {list(MODELS)}")
        return v

    @field_validator("version")
    @classmethod
    def _version_in_enum(cls, v: str) -> str:
        if v not in VERSIONS:
            raise ValueError(f"version must be one of {list(VERSIONS)}")
        return v

    @field_validator("effort")
    @classmethod
    def _effort_in_enum(cls, v: str) -> str:
        if v not in EFFORTS:
            raise ValueError(f"effort must be one of {list(EFFORTS)}")
        return v


class ClaudeCodeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orchestrator: AgentSettings
    agents: list[AgentSettings] = Field(min_length=1, max_length=8)
    # MCP-proxy connections to expose as context for this org's
    # runs. Optional with empty-default so rows without it still parse.
    mcp_proxy_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_agent_names(self) -> ClaudeCodeSettings:
        names = [a.name for a in self.agents]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"sub-agent names must be unique; duplicates: {duplicates}")
        return self


def validate_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Public validator used by `ClaudeCodePlugin.validate_settings`. Returns
    a normalized dict; raises `ValueError` (with Pydantic detail attached
    via `__cause__`) on invalid input."""
    try:
        parsed = ClaudeCodeSettings.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError subclasses Exception
        raise ValueError(str(exc)) from exc
    return parsed.model_dump(mode="python")
