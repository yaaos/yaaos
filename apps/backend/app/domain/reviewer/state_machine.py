"""Pure transition functions for `FindingState`.

The aggregate decides whether evidence (a classifier output, a verify-fix
result) is strong enough to transition; this module's job is to validate
that the requested transition is allowed.

| From → To | Trigger |
|---|---|
| (new) → open | New fingerprint observed in a review. |
| open → acknowledged | Developer ack (with `AckKind`). |
| open → resolved_confirmed | verify_fix returned "not present" ≥ threshold. |
| open → resolved_unverified | Anchor gone, no verify-fix possible. |
| open → stale | stale_check returned "no longer applies" ≥ threshold. |
| resolved_* / stale / acknowledged | Terminal in this PR. |

`superseded` + `acknowledged → open` are not supported in the POC.
"""

from __future__ import annotations

from app.domain.reviewer.types import FindingState

_ALLOWED: dict[FindingState, frozenset[FindingState]] = {
    FindingState.OPEN: frozenset(
        {
            FindingState.ACKNOWLEDGED,
            FindingState.RESOLVED_CONFIRMED,
            FindingState.RESOLVED_UNVERIFIED,
            FindingState.STALE,
        }
    ),
    # Terminal states stay put for POC.
    FindingState.ACKNOWLEDGED: frozenset(),
    FindingState.RESOLVED_CONFIRMED: frozenset(),
    FindingState.RESOLVED_UNVERIFIED: frozenset(),
    FindingState.STALE: frozenset(),
}


class IllegalTransition(ValueError):
    """Raised when a caller asks for a transition the state machine forbids."""


def can_transition(from_state: FindingState, to_state: FindingState) -> bool:
    """True iff `from_state → to_state` is an allowed transition."""
    return to_state in _ALLOWED.get(from_state, frozenset())


def transition(from_state: FindingState, to_state: FindingState) -> FindingState:
    """Validate and return `to_state`. Raises `IllegalTransition` if forbidden.

    Use this at the aggregate boundary — never mutate `Finding.state` directly.
    """
    if not can_transition(from_state, to_state):
        raise IllegalTransition(f"cannot move {from_state.value} -> {to_state.value}")
    return to_state
