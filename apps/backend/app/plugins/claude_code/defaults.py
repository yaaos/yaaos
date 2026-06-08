"""Allowed enumeration values for the `claude_code` plugin.

Surfaced to the UI as dropdown options via the defaults endpoint.
"""

from __future__ import annotations

# Allowed model + effort enumerations. Surfaced to the UI as dropdown
# options via `GET /api/claude_code/defaults`.
MODELS: tuple[str, ...] = ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")

__all__ = ["EFFORTS", "MODELS"]
