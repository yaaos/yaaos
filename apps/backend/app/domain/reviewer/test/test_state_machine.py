"""Unit tests for `state_machine.py` — every transition + every reject case."""

from __future__ import annotations

import pytest

from app.domain.reviewer.state_machine import IllegalTransition, can_transition, transition
from app.domain.reviewer.types import FindingState


def test_open_to_acknowledged_allowed() -> None:
    assert can_transition(FindingState.OPEN, FindingState.ACKNOWLEDGED)
    assert transition(FindingState.OPEN, FindingState.ACKNOWLEDGED) == FindingState.ACKNOWLEDGED


def test_open_to_resolved_confirmed_allowed() -> None:
    assert transition(FindingState.OPEN, FindingState.RESOLVED_CONFIRMED) == FindingState.RESOLVED_CONFIRMED


def test_open_to_resolved_unverified_allowed() -> None:
    assert transition(FindingState.OPEN, FindingState.RESOLVED_UNVERIFIED) == FindingState.RESOLVED_UNVERIFIED


def test_open_to_stale_allowed() -> None:
    assert transition(FindingState.OPEN, FindingState.STALE) == FindingState.STALE


@pytest.mark.parametrize(
    "terminal",
    [
        FindingState.ACKNOWLEDGED,
        FindingState.RESOLVED_CONFIRMED,
        FindingState.RESOLVED_UNVERIFIED,
        FindingState.STALE,
    ],
)
def test_terminal_states_reject_all_transitions(terminal: FindingState) -> None:
    for target in FindingState:
        if target == terminal:
            continue
        assert not can_transition(terminal, target)
        with pytest.raises(IllegalTransition):
            transition(terminal, target)


def test_open_to_open_rejected() -> None:
    """No self-transition for OPEN — re-observation isn't a state change."""
    assert not can_transition(FindingState.OPEN, FindingState.OPEN)


def test_is_terminal_predicate() -> None:
    assert not FindingState.OPEN.is_terminal
    assert FindingState.ACKNOWLEDGED.is_terminal
    assert FindingState.RESOLVED_CONFIRMED.is_terminal
    assert FindingState.RESOLVED_UNVERIFIED.is_terminal
    assert FindingState.STALE.is_terminal
