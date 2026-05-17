"""In-memory `AggregateRepository` used by aggregate unit tests.

Holds one process-global store keyed by `pr_id`. `load` returns a fresh
aggregate populated from the store; `save` drains the aggregate's pending
writes back into the store.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.types import (
    AcknowledgmentDecision,
    CommentMessage,
    CommentThread,
    Finding,
    FindingObservation,
    Review,
)


@dataclass
class _Store:
    reviews: dict[uuid.UUID, Review] = field(default_factory=dict)
    findings: dict[uuid.UUID, Finding] = field(default_factory=dict)
    observations: list[FindingObservation] = field(default_factory=list)
    threads: dict[uuid.UUID, CommentThread] = field(default_factory=dict)
    messages: list[CommentMessage] = field(default_factory=list)
    acks: list[AcknowledgmentDecision] = field(default_factory=list)
    org_id: uuid.UUID | None = None


class InMemoryAggregateRepository:
    """Conforms to `AggregateRepository` Protocol. Test-only."""

    def __init__(self) -> None:
        self._stores: dict[uuid.UUID, _Store] = {}

    async def load(self, *, pr_id: uuid.UUID, org_id: uuid.UUID) -> PRReviewAggregate:
        store = self._stores.setdefault(pr_id, _Store(org_id=org_id))
        store.org_id = org_id
        return PRReviewAggregate(
            pr_id=pr_id,
            org_id=org_id,
            reviews=[
                Review(
                    id=r.id,
                    pr_id=r.pr_id,
                    org_id=r.org_id,
                    sequence_number=r.sequence_number,
                    trigger_reason=r.trigger_reason,
                    scope=r.scope,
                    commit_sha_at_start=r.commit_sha_at_start,
                    status=r.status,
                    superseded_by_review_id=r.superseded_by_review_id,
                    pending_replay=r.pending_replay,
                    created_at=r.created_at,
                )
                for r in store.reviews.values()
            ],
            findings=[
                Finding(
                    id=f.id,
                    pr_id=f.pr_id,
                    org_id=f.org_id,
                    fingerprint=f.fingerprint,
                    rule_id=f.rule_id,
                    title=f.title,
                    body=f.body,
                    rationale=f.rationale,
                    concrete_failure_scenario=f.concrete_failure_scenario,
                    confidence=f.confidence,
                    severity=f.severity,
                    state=f.state,
                    current_anchor=f.current_anchor,
                    source_agent=f.source_agent,
                    first_seen_review_id=f.first_seen_review_id,
                    last_observed_review_id=f.last_observed_review_id,
                    created_at=f.created_at,
                    updated_at=f.updated_at,
                )
                for f in store.findings.values()
            ],
            observations=list(store.observations),
            threads=list(store.threads.values()),
            messages=list(store.messages),
            acks=list(store.acks),
        )

    async def save(self, aggregate: PRReviewAggregate) -> None:
        store = self._stores.setdefault(aggregate.pr_id, _Store(org_id=aggregate.org_id))
        pending = aggregate.pop_pending()
        for r in pending.new_reviews:
            store.reviews[r.id] = r
        for r in pending.updated_reviews:
            store.reviews[r.id] = r
        for f in pending.new_findings:
            store.findings[f.id] = f
        for f in pending.updated_findings:
            store.findings[f.id] = f
        store.observations.extend(pending.new_observations)
        for t in pending.new_threads:
            store.threads[t.id] = t
        store.messages.extend(pending.new_messages)
        store.acks.extend(pending.new_acks)

    def reset(self) -> None:
        self._stores.clear()
