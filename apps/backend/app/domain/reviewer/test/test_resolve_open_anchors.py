"""Anchor re-resolution wired into incremental review.

Before the LLM stale_check runs, the deterministic `resolve_anchor` should
check each open finding in touched files. Three outcomes:

- **gone** (file deleted, hash absent, or ambiguous) → finding moves to
  `resolved_unverified`. The LLM stale_check still has a chance to override
  to `stale` if it knows better, but at minimum we no longer dangle.
- **moved** (hash found at a new line range) → `update_anchor` on the
  aggregate, finding stays OPEN, the caller is told to run verify_fix on it.
- **unchanged** → no-op (same lines, just stamps new commit_sha).

This file tests the pure helper. Wiring into the `IncrementalReview`
engine command is covered indirectly by the end-to-end push tests.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.anchor import make_anchor
from app.domain.reviewer.incremental_anchor import resolve_open_anchors
from app.domain.reviewer.types import (
    Finding,
    FindingFingerprint,
    FindingState,
)


def _file_lines(*, anchor_at: int, total: int = 30) -> list[str]:
    """Build a file where the anchor 'block' lives at a specific line."""
    lines = [f"unrelated line {i}" for i in range(total)]
    lines[anchor_at - 4 : anchor_at + 3] = [
        "above context 1",
        "above context 2",
        "above context 3",
        "ANCHORED CONTENT",
        "below context 1",
        "below context 2",
        "below context 3",
    ]
    return lines


def _seed_finding(agg: PRReviewAggregate, *, file_path: str, line: int, commit_sha: str) -> Finding:
    """Seed an OPEN finding with an anchor derived from the file content at `line`."""
    file_lines = _file_lines(anchor_at=line)
    anchor = make_anchor(
        file_path=file_path,
        file_lines=file_lines,
        line_start=line,
        line_end=line,
        commit_sha=commit_sha,
    )
    fingerprint = FindingFingerprint(
        file_path=file_path,
        rule_id="r/x",
        anchor_content_hash=anchor.surrounding_content_hash,
        body_gist_hash="gist",
    )
    finding = Finding(
        id=uuid.uuid4(),
        pr_id=agg.pr_id,
        org_id=agg.org_id,
        fingerprint=fingerprint,
        rule_id="r/x",
        title="t",
        body="b",
        rationale="r",
        concrete_failure_scenario="caller invokes f() with stale data; raises ValueError.",
        confidence=90,
        severity="major",
        state=FindingState.OPEN,
        current_anchor=anchor,
        source_agent="test",
        first_seen_review_id=uuid.uuid4(),
        last_observed_review_id=uuid.uuid4(),
        created_at=datetime(2026, 5, 17, tzinfo=UTC),
        updated_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    agg._state.findings[finding.id] = finding  # type: ignore[attr-defined]
    return finding


def test_resolve_open_anchors_moves_anchor_when_block_shifts() -> None:
    """Anchor block moves from line 10 to line 20; helper updates the anchor."""
    agg = PRReviewAggregate(
        pr_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        now=datetime(2026, 5, 17, tzinfo=UTC),
    )
    finding = _seed_finding(agg, file_path="src/foo.py", line=10, commit_sha="old")

    # New content: same anchored block, now sitting at line 20 instead of 10.
    new_lines = _file_lines(anchor_at=20)

    result = resolve_open_anchors(
        agg,
        touched_files={"src/foo.py"},
        read_file=lambda path: new_lines if path == "src/foo.py" else None,
        new_commit_sha="new",
    )

    refreshed = agg._state.findings[finding.id]  # type: ignore[attr-defined]
    assert refreshed.current_anchor.line_start == 20
    assert refreshed.state == FindingState.OPEN
    assert finding.id in result.moved


def test_resolve_open_anchors_marks_unverified_when_file_deleted() -> None:
    agg = PRReviewAggregate(
        pr_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        now=datetime(2026, 5, 17, tzinfo=UTC),
    )
    finding = _seed_finding(agg, file_path="src/gone.py", line=10, commit_sha="old")

    result = resolve_open_anchors(
        agg,
        touched_files={"src/gone.py"},
        read_file=lambda path: None,  # file deleted
        new_commit_sha="new",
    )

    refreshed = agg._state.findings[finding.id]  # type: ignore[attr-defined]
    assert refreshed.state == FindingState.RESOLVED_UNVERIFIED
    assert finding.id in result.gone


def test_resolve_open_anchors_marks_unverified_when_hash_absent() -> None:
    agg = PRReviewAggregate(
        pr_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        now=datetime(2026, 5, 17, tzinfo=UTC),
    )
    finding = _seed_finding(agg, file_path="src/foo.py", line=10, commit_sha="old")

    # New content: the anchored block is gone entirely.
    new_lines = [f"completely different line {i}" for i in range(30)]

    result = resolve_open_anchors(
        agg,
        touched_files={"src/foo.py"},
        read_file=lambda path: new_lines,
        new_commit_sha="new",
    )

    refreshed = agg._state.findings[finding.id]  # type: ignore[attr-defined]
    assert refreshed.state == FindingState.RESOLVED_UNVERIFIED
    assert finding.id in result.gone


def test_resolve_open_anchors_noop_when_anchor_unchanged() -> None:
    agg = PRReviewAggregate(
        pr_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        now=datetime(2026, 5, 17, tzinfo=UTC),
    )
    finding = _seed_finding(agg, file_path="src/foo.py", line=10, commit_sha="old")

    # Same content; only commit_sha differs.
    same_lines = _file_lines(anchor_at=10)

    result = resolve_open_anchors(
        agg,
        touched_files={"src/foo.py"},
        read_file=lambda path: same_lines,
        new_commit_sha="new",
    )

    refreshed = agg._state.findings[finding.id]  # type: ignore[attr-defined]
    assert refreshed.current_anchor.line_start == 10
    assert refreshed.state == FindingState.OPEN
    assert finding.id not in result.moved
    assert finding.id not in result.gone
