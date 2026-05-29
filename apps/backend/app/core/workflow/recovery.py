"""Recovery-policy registry — maps AgentCommand failure labels to WorkflowCommand kinds.

The engine consults this registry (Tier-1 recovery) before falling through to
Tier-2 retry or Tier-3 terminal transitions. Producers (e.g. `core/workspace`)
register their policies at import; the engine reads them via `get_recovery_policy`.
"""

from __future__ import annotations

_RECOVERY_POLICIES: dict[str, str] = {}


def register_recovery_policy(*, failure_label: str, command_kind: str) -> None:
    """Map an AgentCommand failure label (e.g. `auth_expired`) to a
    WorkflowCommand kind the engine inserts before re-dispatching the
    original step. Idempotent for the same mapping; raises on conflict so
    typos surface at boot."""
    existing = _RECOVERY_POLICIES.get(failure_label)
    if existing is not None and existing != command_kind:
        raise ValueError(
            f"recovery policy for '{failure_label}' already maps to '{existing}', "
            f"refusing to remap to '{command_kind}'"
        )
    _RECOVERY_POLICIES[failure_label] = command_kind


def get_recovery_policy(failure_label: str) -> str | None:
    """Look up the WorkflowCommand kind that recovers `failure_label`, or
    None if no policy is registered (engine falls through to Tier-2 retry)."""
    return _RECOVERY_POLICIES.get(failure_label)


def registered_recovery_labels() -> list[str]:
    """Return all registered failure labels in sorted order."""
    return sorted(_RECOVERY_POLICIES.keys())


def clear_recovery_policies() -> None:
    """Clear all registered recovery policies. For test isolation only."""
    _RECOVERY_POLICIES.clear()
