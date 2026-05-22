"""Reviewer-wide constants extracted from the legacy `queue.py` so the
new workflow-engine path + the legacy runner can share one source of truth.

Once `queue.py` is deleted, this module is the home for these values —
no re-binding shims required.
"""

from __future__ import annotations

from uuid import UUID

# Default org id used by the M01 single-tenant POC for rows that predate
# multi-tenant scoping. Anywhere a real `org_id` flows it should take
# precedence; this constant is the fallback identity only.
M01_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

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
    "M01_ORG_ID",
    "REVIEWER_TAG",
]
