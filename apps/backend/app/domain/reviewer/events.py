"""Domain events emitted by the `PRReviewAggregate` (plan §2.4).

Plain dataclasses; the aggregate appends them to its internal event list as
side effects accumulate. `service.py` drains them after persisting the
aggregate and dispatches to `core/events` subscribers + the SSE bus.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.reviewer.types import (
    AckKind,
    CodeAnchor,
    FindingState,
    ReplyIntent,
    ReviewScope,
    ReviewTrigger,
)


@dataclass(frozen=True)
class ReviewRequested:
    review_id: uuid.UUID
    pr_id: uuid.UUID
    trigger: ReviewTrigger
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
    findings_observed: list[uuid.UUID]


@dataclass(frozen=True)
class ReviewFailed:
    review_id: uuid.UUID
    pr_id: uuid.UUID
    reason: str


@dataclass(frozen=True)
class ReviewSuperseded:
    review_id: uuid.UUID
    pr_id: uuid.UUID
    by_review_id: uuid.UUID


@dataclass(frozen=True)
class FindingRaised:
    finding_id: uuid.UUID
    pr_id: uuid.UUID


@dataclass(frozen=True)
class FindingReObserved:
    finding_id: uuid.UUID
    review_id: uuid.UUID


@dataclass(frozen=True)
class FindingAnchorUpdated:
    finding_id: uuid.UUID
    new_anchor: CodeAnchor


@dataclass(frozen=True)
class FindingStateChanged:
    finding_id: uuid.UUID
    from_state: FindingState
    to_state: FindingState


@dataclass(frozen=True)
class FindingAcknowledged:
    finding_id: uuid.UUID
    ack_id: uuid.UUID
    kind: AckKind


@dataclass(frozen=True)
class FindingResolutionDetected:
    finding_id: uuid.UUID
    kind: FindingState  # RESOLVED_CONFIRMED | RESOLVED_UNVERIFIED


@dataclass(frozen=True)
class FindingStaleDetected:
    finding_id: uuid.UUID


@dataclass(frozen=True)
class CommentReplyReceived:
    thread_id: uuid.UUID
    message_id: uuid.UUID
    classified_intent: ReplyIntent


@dataclass(frozen=True)
class AgentReplyPosted:
    thread_id: uuid.UUID
    message_id: uuid.UUID


DomainEvent = (
    ReviewRequested
    | ReviewStarted
    | ReviewCompleted
    | ReviewFailed
    | ReviewSuperseded
    | FindingRaised
    | FindingReObserved
    | FindingAnchorUpdated
    | FindingStateChanged
    | FindingAcknowledged
    | FindingResolutionDetected
    | FindingStaleDetected
    | CommentReplyReceived
    | AgentReplyPosted
)
