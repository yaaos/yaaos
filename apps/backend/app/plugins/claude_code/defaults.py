"""Allowed enumeration values for the `claude_code` plugin.

Surfaced to the UI via `ClaudeCodePlugin.stage_options()`, which the
`GET /api/coding-agents` list endpoint attaches to each installed-agent row.
"""

from __future__ import annotations

# Allowed model + effort enumerations.
MODELS: tuple[str, ...] = ("claude-sonnet-5", "claude-opus-4-8", "claude-fable-5", "claude-haiku-4-5")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

__all__ = ["EFFORTS", "MODELS"]
