"""Default orchestrator + sub-agent configs for the Claude Code plugin.

These are the values copied into `org_coding_agents.settings` JSONB at
install time. The settings UI calls `GET /api/orgs/{slug}/coding-agents/
claude_code/defaults` at request time to render "Reset to default" + the
"Overridden" badges; the endpoint imports this module at *request* time
so a code change to defaults is reflected on the next request — never
cached at module load.

Shape:
    {orchestrator: {name, prompt, model, version, effort, updated_at},
     agents: [{name, prompt, model, version, effort, updated_at}, ...]}

`updated_at` for defaults is the empty string; the API consumer treats
absence/empty as "never overridden".
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Allowed model + effort enumerations. Surfaced to the UI as dropdown
# options via the same defaults endpoint, shipped inline with the
# defaults so the SPA doesn't need a separate metadata fetch.
MODELS: tuple[str, ...] = ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5")
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
VERSIONS: tuple[str, ...] = ("latest", "stable")

_ORCHESTRATOR: dict[str, Any] = {
    "name": "Orchestrator",
    "prompt": (
        "You are the yaaos parent reviewer. Dispatch the registered sub-agents "
        "via the Task tool, then synthesize their findings into a single "
        "review. Do not write code; do not edit files."
    ),
    "model": "claude-sonnet-4-6",
    "version": "latest",
    "effort": "medium",
    "updated_at": "",
}

_AGENTS: list[dict[str, Any]] = [
    {
        "name": "yaaos-architecture",
        "prompt": (
            "Review the diff for architectural concerns: module boundaries, "
            "abstractions, separation of concerns. Surface concrete must-fix / "
            "suggestion findings; do not nitpick style."
        ),
        "model": "claude-sonnet-4-6",
        "version": "latest",
        "effort": "medium",
        "updated_at": "",
    },
    {
        "name": "yaaos-security",
        "prompt": (
            "Review the diff for security concerns: auth, injection, secret "
            "handling, crypto misuse. Be conservative — only flag concrete "
            "vulnerabilities you can point to in the code."
        ),
        "model": "claude-sonnet-4-6",
        "version": "latest",
        "effort": "medium",
        "updated_at": "",
    },
    {
        "name": "yaaos-tests",
        "prompt": (
            "Review the diff for test coverage. Every behavior change ships "
            "with tests; no mocks in tests; no over-mocking. Surface gaps."
        ),
        "model": "claude-sonnet-4-6",
        "version": "latest",
        "effort": "low",
        "updated_at": "",
    },
]


def get_defaults() -> dict[str, Any]:
    """Return a deep copy so callers can't mutate module state by accident."""
    return {
        "orchestrator": deepcopy(_ORCHESTRATOR),
        "agents": deepcopy(_AGENTS),
        "models": list(MODELS),
        "versions": list(VERSIONS),
        "efforts": list(EFFORTS),
    }


__all__ = ["EFFORTS", "MODELS", "VERSIONS", "get_defaults"]
