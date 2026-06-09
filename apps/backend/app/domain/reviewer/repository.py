"""SQLAlchemy helpers for the reviewer's Read side.

`_review_from_row` and `_finding_from_row` convert ORM rows to domain
value objects. `SqlAlchemyAggregateRepository` loads reviews + findings for
a PR and provides the save hook for the aggregate's pending writes.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.models import FindingRow, ReviewRow
from app.domain.reviewer.types import (
    Finding,
    Review,
    ReviewScope,
    ReviewScopeKind,
)


def _review_from_row(row: ReviewRow) -> Review:
    scope = ReviewScope(
        kind=row.scope_kind,
        base_sha="",
        head_sha=row.commit_sha_at_start or "",
    )
    return Review(
        id=row.id,
        pr_id=row.pr_id,
        org_id=row.org_id,
        sequence_number=row.sequence_number,
        trigger_reason=row.trigger_reason,
        scope=scope,
        commit_sha_at_start=row.commit_sha_at_start or "",
        status=row.status,
        created_at=row.created_at,
    )


def _finding_from_row(row: FindingRow) -> Finding:
    return Finding(
        id=row.id,
        pr_id=row.pr_id,
        org_id=row.org_id,
        review_id=row.review_id,
        finding_display_id=row.finding_display_id,
        category=row.category,
        severity=row.severity,  # type: ignore[arg-type]
        confidence=row.confidence,  # type: ignore[arg-type]
        rationale=row.rationale,
        rule_violated=row.rule_violated,
        rule_source=row.rule_source,
        suggested_fix=row.suggested_fix,
        file=row.file,
        line=row.line,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyAggregateRepository:
    """`AggregateRepository` backed by an `AsyncSession`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, *, pr_id: uuid.UUID, org_id: uuid.UUID) -> PRReviewAggregate:
        review_rows = list(
            (
                await self._session.execute(
                    select(ReviewRow).where(ReviewRow.pr_id == pr_id, ReviewRow.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        finding_rows = list(
            (
                await self._session.execute(
                    select(FindingRow).where(FindingRow.pr_id == pr_id, FindingRow.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        return PRReviewAggregate(
            pr_id=pr_id,
            org_id=org_id,
            reviews=[_review_from_row(r) for r in review_rows],
            findings=[_finding_from_row(r) for r in finding_rows],
        )

    async def save(self, aggregate: PRReviewAggregate) -> None:
        pending = aggregate.pop_pending()

        for r in pending.new_reviews:
            row = await self._session.get(ReviewRow, r.id)
            if row is None:
                self._session.add(
                    ReviewRow(
                        id=r.id,
                        org_id=r.org_id,
                        pr_id=r.pr_id,
                        sequence_number=r.sequence_number,
                        trigger_reason=r.trigger_reason,
                        scope_kind=r.scope.kind if r.scope else ReviewScopeKind.FULL,
                        commit_sha_at_start=r.commit_sha_at_start,
                        status=r.status,
                    )
                )
            else:
                row.status = r.status
                row.commit_sha_at_start = r.commit_sha_at_start

        for r in pending.updated_reviews:
            row = await self._session.get(ReviewRow, r.id)
            if row is not None:
                row.status = r.status
                row.commit_sha_at_start = r.commit_sha_at_start

        await self._session.flush()

        for f in pending.new_findings:
            self._session.add(
                FindingRow(
                    id=f.id,
                    org_id=f.org_id,
                    pr_id=f.pr_id,
                    review_id=f.review_id,
                    finding_display_id=f.finding_display_id,
                    category=f.category,
                    severity=str(f.severity),
                    confidence=str(f.confidence),
                    rationale=f.rationale,
                    rule_violated=f.rule_violated,
                    rule_source=f.rule_source,
                    suggested_fix=f.suggested_fix,
                    file=f.file,
                    line=f.line,
                )
            )

        if pending.new_findings:
            await self._session.flush()
