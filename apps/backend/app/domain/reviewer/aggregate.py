"""`PRReviewAggregate` — minimal consistency boundary for one PR's review state.

Loads `Review`s and `Finding`s for a PR. Consumers call `start_review`,
`complete_review`, `fail_review`, and `raise_finding`. The aggregate no
longer runs an admission/fingerprint gate — `publish.py` owns that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.reviewer.types import (
    Finding,
    Review,
    ReviewScope,
)


@dataclass
class _PRReviewState:
    """Loaded state for one PR."""

    pr_id: uuid.UUID
    org_id: uuid.UUID
    reviews: dict[uuid.UUID, Review] = field(default_factory=dict)
    findings: dict[uuid.UUID, Finding] = field(default_factory=dict)


@dataclass
class _PendingWrites:
    """Changes since load. Repository drains this on save."""

    new_reviews: list[Review] = field(default_factory=list)
    updated_reviews: list[Review] = field(default_factory=list)
    new_findings: list[Finding] = field(default_factory=list)


class PRReviewAggregate:
    """Application-side handle on one PR's review state."""

    def __init__(
        self,
        *,
        pr_id: uuid.UUID,
        org_id: uuid.UUID,
        reviews: list[Review] | None = None,
        findings: list[Finding] | None = None,
        now: datetime | None = None,
    ) -> None:
        self._state = _PRReviewState(pr_id=pr_id, org_id=org_id)
        for r in reviews or []:
            self._state.reviews[r.id] = r
        for f in findings or []:
            self._state.findings[f.id] = f
        self._pending = _PendingWrites()
        self._now = now or datetime.now(UTC)

    @property
    def pr_id(self) -> uuid.UUID:
        return self._state.pr_id

    @property
    def org_id(self) -> uuid.UUID:
        return self._state.org_id

    @property
    def reviews(self) -> list[Review]:
        return sorted(self._state.reviews.values(), key=lambda r: r.sequence_number)

    @property
    def findings(self) -> list[Finding]:
        return list(self._state.findings.values())

    @property
    def pending(self) -> _PendingWrites:
        return self._pending

    def pop_pending(self) -> _PendingWrites:
        out = self._pending
        self._pending = _PendingWrites()
        return out

    # ─── Reviews ────────────────────────────────────────────────────────────

    def start_review(
        self,
        *,
        trigger: str,
        scope: ReviewScope,
        commit_sha: str,
        review_id: uuid.UUID | None = None,
    ) -> Review:
        review_id = review_id or uuid.uuid7()
        sequence_number = max((r.sequence_number for r in self._state.reviews.values()), default=0) + 1
        review = Review(
            id=review_id,
            pr_id=self._state.pr_id,
            org_id=self._state.org_id,
            sequence_number=sequence_number,
            trigger_reason=trigger,
            scope=scope,
            commit_sha_at_start=commit_sha,
            status="queued",
            created_at=self._now,
        )
        self._state.reviews[review_id] = review
        self._pending.new_reviews.append(review)
        return review

    def mark_review_running(self, review_id: uuid.UUID, commit_sha: str) -> None:
        review = self._state.reviews[review_id]
        review.status = "running"
        review.commit_sha_at_start = commit_sha
        self._pending.updated_reviews.append(review)

    def complete_review(self, review_id: uuid.UUID) -> None:
        review = self._state.reviews[review_id]
        review.status = "done"
        self._pending.updated_reviews.append(review)

    def fail_review(self, review_id: uuid.UUID, reason: str) -> None:
        del reason
        review = self._state.reviews[review_id]
        review.status = "failed"
        self._pending.updated_reviews.append(review)

    def latest_review(self) -> Review | None:
        if not self._state.reviews:
            return None
        return max(self._state.reviews.values(), key=lambda r: r.sequence_number)
