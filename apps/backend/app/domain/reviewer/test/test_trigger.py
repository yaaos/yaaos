"""Unit tests for `trigger.py` — every rule, in order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.reviewer.trigger import (
    Debounce,
    Run,
    Skip,
    TriggerInputs,
    decide_trigger,
    humanize_skip,
)


def _inputs(**overrides) -> TriggerInputs:  # type: ignore[no-untyped-def]
    defaults: dict = {
        "pr_is_draft": False,
        "last_reviewed_sha": "old_sha",
        "head_sha": "new_sha",
        "in_flight_review_id": None,
        "new_commit_messages": ["fix: tweak"],
        "last_reviewed_sha_is_ancestor": True,
        "last_push_at": None,
        "now": datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC),
        "debounce_window_seconds": 30,
    }
    defaults.update(overrides)
    return TriggerInputs(**defaults)


def test_draft_pr_skipped() -> None:
    decision = decide_trigger(_inputs(pr_is_draft=True))
    assert decision == Skip(reason="draft")


def test_first_review_skipped_as_history_changed() -> None:
    """No last_reviewed_sha → can't compute incremental scope → route to manual."""
    decision = decide_trigger(_inputs(last_reviewed_sha=None))
    assert decision == Skip(reason="history_changed")


def test_force_push_detected_as_history_changed() -> None:
    decision = decide_trigger(_inputs(last_reviewed_sha_is_ancestor=False))
    assert decision == Skip(reason="history_changed")


def test_base_merge_skipped() -> None:
    decision = decide_trigger(_inputs(new_commit_messages=["Merge branch 'main' into feature/x"]))
    assert decision == Skip(reason="base_merged")


def test_pr_merge_skipped() -> None:
    decision = decide_trigger(_inputs(new_commit_messages=["Merge pull request #42 from acme/branch"]))
    assert decision == Skip(reason="base_merged")


def test_in_flight_skipped() -> None:
    decision = decide_trigger(_inputs(in_flight_review_id="r123"))
    assert decision == Skip(reason="in_flight")


def test_push_within_debounce_returns_debounce_with_remaining_time() -> None:
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    decision = decide_trigger(
        _inputs(now=now, last_push_at=now - timedelta(seconds=10), debounce_window_seconds=30)
    )
    assert isinstance(decision, Debounce)
    assert 19 < decision.seconds_remaining <= 20


def test_push_outside_debounce_runs_incremental() -> None:
    now = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)
    decision = decide_trigger(_inputs(now=now, last_push_at=now - timedelta(seconds=120)))
    assert isinstance(decision, Run)
    assert decision.scope.kind == "incremental"
    assert decision.scope.base_sha == "old_sha"
    assert decision.scope.head_sha == "new_sha"


def test_clean_path_no_recent_push_runs_immediately() -> None:
    decision = decide_trigger(_inputs(last_push_at=None))
    assert isinstance(decision, Run)


def test_humanize_skip_covers_every_reason() -> None:
    for reason in ("draft", "history_changed", "base_merged", "in_flight"):
        msg = humanize_skip(reason)  # type: ignore[arg-type]
        assert msg and isinstance(msg, str)
