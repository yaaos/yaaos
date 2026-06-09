"""`PRReviewAggregate` — minimal consistency boundary for one PR's review state.

Tests for start_review, mark_review_running, complete_review, fail_review,
latest_review, and pending-writes drain.
"""

from __future__ import annotations

import uuid

from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.types import ReviewScope


def _agg(**kwargs) -> PRReviewAggregate:  # type: ignore[no-untyped-def]
    defaults = dict(pr_id=uuid.uuid4(), org_id=uuid.uuid4())
    defaults.update(kwargs)
    return PRReviewAggregate(**defaults)


def test_start_review_sequence_numbers_are_monotonic() -> None:
    agg = _agg()
    r1 = agg.start_review(trigger="pr_ready", scope=ReviewScope.full("b", "h"), commit_sha="abc")
    r2 = agg.start_review(trigger="manual", scope=ReviewScope.full("b", "h"), commit_sha="def")
    assert r1.sequence_number == 1
    assert r2.sequence_number == 2


def test_start_review_adds_to_reviews() -> None:
    agg = _agg()
    r = agg.start_review(trigger="pr_ready", scope=ReviewScope.full("b", "h"), commit_sha="abc")
    assert r in agg.reviews
    assert r.status == "queued"


def test_mark_review_running_updates_status() -> None:
    agg = _agg()
    r = agg.start_review(trigger="pr_ready", scope=ReviewScope.full("b", "h"), commit_sha="abc")
    agg.mark_review_running(r.id, "def")
    assert agg.reviews[0].status == "running"
    assert agg.reviews[0].commit_sha_at_start == "def"


def test_complete_review_sets_done() -> None:
    agg = _agg()
    r = agg.start_review(trigger="pr_ready", scope=ReviewScope.full("b", "h"), commit_sha="abc")
    agg.complete_review(r.id)
    assert agg.reviews[0].status == "done"


def test_fail_review_sets_failed() -> None:
    agg = _agg()
    r = agg.start_review(trigger="pr_ready", scope=ReviewScope.full("b", "h"), commit_sha="abc")
    agg.fail_review(r.id, "timeout")
    assert agg.reviews[0].status == "failed"


def test_latest_review_returns_highest_sequence() -> None:
    agg = _agg()
    r1 = agg.start_review(trigger="pr_ready", scope=ReviewScope.full("b", "h"), commit_sha="a")
    r2 = agg.start_review(trigger="manual", scope=ReviewScope.full("b", "h"), commit_sha="b")
    assert agg.latest_review() is r2
    _ = r1  # suppress unused warning


def test_latest_review_none_when_empty() -> None:
    agg = _agg()
    assert agg.latest_review() is None


def test_pop_pending_drains_writes() -> None:
    agg = _agg()
    r = agg.start_review(trigger="pr_ready", scope=ReviewScope.full("b", "h"), commit_sha="abc")
    agg.complete_review(r.id)
    pending = agg.pop_pending()
    assert len(pending.new_reviews) == 1
    assert len(pending.updated_reviews) == 1
    # After drain, pending is empty.
    empty = agg.pop_pending()
    assert not empty.new_reviews
    assert not empty.updated_reviews
