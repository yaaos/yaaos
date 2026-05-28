"""Reviewer-wide constants — one source of truth for the workflow-engine
path and any runner that needs them.
"""

from __future__ import annotations

# Hard-coded reviewer identity. There's only one reviewer (the parent
# agent that dispatches subagents); we use this tag on the top-level
# GitHub review body. Per-comment prefixes come from each finding's
# `source_agent` field.
REVIEWER_TAG = "yaaos"

# Hard-coded coding-agent plugin id. The reviewer ships claude_code as
# the only coding agent today; future plugins (codex, aider) would
# require a settings row + per-org override.
CODING_AGENT_PLUGIN_ID = "claude_code"

# Default Claude Code model + effort. Mirror the constants in
# `plugins/claude_code` (`_MODEL`, `_EFFORT`); duplicated to keep the
# Tach layering clean — `domain/reviewer` cannot import from `plugins/*`.
# Future UI configuration replaces both copies with a settings row.
DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "medium"


__all__ = [
    "CODING_AGENT_PLUGIN_ID",
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "REVIEWER_TAG",
]
