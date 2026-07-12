"""Allowed enumeration values for the `codex` plugin.

Surfaced to the UI via `CodexPlugin.stage_options()`, which the
`GET /api/coding-agents` list endpoint attaches to each installed-agent row.
"""

from __future__ import annotations

# Allowed model + effort enumerations.
# Model IDs in this family change frequently; gpt-4.1 and codex-mini-latest
# are the canonical production defaults as of July 2026.
MODELS: tuple[str, ...] = ("codex-mini-latest", "gpt-4.1", "o4-mini", "o3")
EFFORTS: tuple[str, ...] = ("low", "medium", "high")

__all__ = ["EFFORTS", "MODELS"]
