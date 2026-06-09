"""Domain events emitted by reviewer operations.

Plain dataclasses; the reviewer dispatches them to the SSE bus via
`service.dispatch_events` after each successful persist.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.reviewer.types import ReviewScope


@dataclass(frozen=True)
class ReviewRequested:
    review_id: uuid.UUID
    pr_id: uuid.UUID
    trigger: str
    scope: ReviewScope


@dataclass(frozen=True)
class ReviewStarted:
    review_id: uuid.UUID
    pr_id: uuid.UUID
    commit_sha: str


@dataclass(frozen=True)
class ReviewCompleted:
    review_id: uuid.UUID
    pr_id: uuid.UUID
    findings_count: int


@dataclass(frozen=True)
class ReviewFailed:
    review_id: uuid.UUID
    pr_id: uuid.UUID
    reason: str


@dataclass(frozen=True)
class FindingRaised:
    finding_id: uuid.UUID
    pr_id: uuid.UUID
    severity: str
    category: str


DomainEvent = ReviewRequested | ReviewStarted | ReviewCompleted | ReviewFailed | FindingRaised
