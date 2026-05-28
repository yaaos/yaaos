"""`dispatch_events` — after-commit SSE publish via `publish_general_after_commit`.

Tests the two critical invariants:
1. A finding state transition draining through `dispatch_events` + commit → the
   corresponding `GeneralEventKind` event arrives on `subscribe_general(org_id)`.
2. Same setup but rollback → no event arrives (rolled-back transactions must never
   emit phantom SPA events).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.sse import GeneralEventKind, reset_pubsub, subscribe_general
from app.domain.reviewer.aggregate import PRReviewAggregate, RawFinding
from app.domain.reviewer.service import dispatch_events
from app.domain.reviewer.types import (
    CodeAnchor,
    FindingFingerprint,
    ReviewScope,
    ReviewTrigger,
)

pytestmark = pytest.mark.usefixtures("redis_or_skip")


def _agg(org_id: uuid.UUID) -> PRReviewAggregate:
    return PRReviewAggregate(pr_id=uuid.uuid4(), org_id=org_id, now=datetime(2026, 5, 28, tzinfo=UTC))


def _seed_finding(agg: PRReviewAggregate) -> None:
    """Seed the aggregate with one admitted finding so state transitions emit events."""
    review = agg.start_review(
        trigger=ReviewTrigger.PR_READY,
        scope=ReviewScope.full(base_sha="b", head_sha="h"),
        commit_sha="h",
    )
    fp = FindingFingerprint(
        file_path="src/foo.py",
        rule_id="r/test",
        anchor_content_hash="anc",
        body_gist_hash="gist",
    )
    rf = RawFinding(
        fingerprint=fp,
        rule_id="r/test",
        title="t",
        body="b",
        rationale="r",
        concrete_failure_scenario="This scenario describes a concrete failure path.",
        confidence=90,
        severity="major",
        anchor=CodeAnchor(
            file_path="src/foo.py",
            line_start=1,
            line_end=3,
            surrounding_content_hash="surr",
            commit_sha="h",
        ),
        source_agent="claude_code",
    )
    agg.post_process_raw_findings(review.id, [rf])
    agg.complete_review(review.id, [f.id for f in agg.findings])


@pytest.mark.asyncio
@pytest.mark.service
async def test_dispatch_events_emits_after_commit(db_session) -> None:  # type: ignore[no-untyped-def]
    """Finding state transition through `dispatch_events` + commit → event on subscribe_general.

    `publish_general_after_commit` stashes events on the SQLAlchemy session and
    flushes them on `after_commit`. The subscriber must be registered BEFORE the
    commit fires — small sleep ensures the Redis SUBSCRIBE round-trip completes.
    """
    reset_pubsub()
    org_id = uuid.uuid4()

    agg = _agg(org_id)
    _seed_finding(agg)

    received: list[dict] = []

    async def _reader() -> None:
        async for event in subscribe_general(org_id):
            received.append(event)
            if len(received) >= 1:
                return

    reader_task = asyncio.create_task(_reader())
    await asyncio.sleep(0.05)

    async with org_context(org_id, ActorKind.SYSTEM):
        dispatch_events(db_session, aggregate=agg)
        await db_session.commit()

    await asyncio.wait_for(reader_task, timeout=3.0)

    assert len(received) >= 1
    kinds = {e["kind"] for e in received}
    # complete_review emits ReviewCompleted + FindingRaised; either is fine.
    assert kinds & {
        GeneralEventKind.REVIEW_COMPLETED.value,
        GeneralEventKind.FINDING_RAISED.value,
    }, f"Expected at least one reviewer event kind, got: {kinds}"
    assert all("ts" in e for e in received), "every SSE event must carry a ts field"


@pytest.mark.asyncio
@pytest.mark.service
async def test_dispatch_events_rollback_emits_nothing(db_session) -> None:  # type: ignore[no-untyped-def]
    """Finding state transition through `dispatch_events` + rollback → no event on subscribe_general.

    `publish_general_after_commit` only flushes on `after_commit`; a rollback
    silently discards the stash.
    """
    reset_pubsub()
    org_id = uuid.uuid4()

    agg = _agg(org_id)
    _seed_finding(agg)

    received: list[dict] = []

    async def _reader() -> None:
        async for event in subscribe_general(org_id):
            received.append(event)
            if len(received) >= 1:
                return

    reader_task = asyncio.create_task(_reader())
    await asyncio.sleep(0.05)

    async with org_context(org_id, ActorKind.SYSTEM):
        dispatch_events(db_session, aggregate=agg)
        await db_session.rollback()

    # Give a brief window for any phantom event to arrive (it should not).
    try:
        await asyncio.wait_for(asyncio.shield(reader_task), timeout=0.3)
    except TimeoutError:
        pass  # expected — no event should arrive

    reader_task.cancel()
    try:
        await reader_task
    except (asyncio.CancelledError, Exception):
        pass

    assert len(received) == 0, f"Rollback must not emit events; got: {received}"
