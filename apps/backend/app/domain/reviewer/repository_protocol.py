"""`AggregateRepository` Protocol — loads + persists `PRReviewAggregate`.

SQLAlchemy and in-memory implementations conform to this Protocol. The
service layer calls `load` inside a transaction (after taking the advisory
lock), mutates the aggregate, then calls `save`.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from app.domain.reviewer.aggregate import PRReviewAggregate


class AggregateRepository(Protocol):
    async def load(self, *, pr_id: uuid.UUID, org_id: uuid.UUID) -> PRReviewAggregate:
        """Load every row tied to this PR; construct + return the aggregate."""
        ...

    async def save(self, aggregate: PRReviewAggregate) -> None:
        """Persist every pending write on the aggregate. Drain its `pending`."""
        ...
