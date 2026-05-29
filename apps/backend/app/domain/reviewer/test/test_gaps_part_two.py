"""Tests covering event dispatch, the public Python API, and eval metrics.

Covered:
- Domain events appended by the aggregate are dispatched to the event bus
  after save().
- Public Python API: `list_reviews_for_pr`, `get_review`,
  `list_findings_for_pr`, `get_thread` are callable from `app.domain.reviewer`.
- Metrics: `acceptance_rate` + `resolved_without_edit_rate` callable
  from the reviewer module.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

# ─── Public Python API ──────────────────────────────────────────────────────


async def _seed_pr(db_session, pr_id: uuid.UUID, org_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    """Minimal pull_requests row so reviews can FK to it."""
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets (id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'acme/web#1', 't', 'in_review', 'github', 'acme/web')"
        ),
        {"id": ticket_id, "org_id": org_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, ticket_id, plugin_id, external_id, repo_external_id, number, title, body,"
            "  author_login, author_type, base_branch, head_branch, base_sha, head_sha,"
            "  is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :tid, 'github', 'acme/web#1', 'acme/web', 1, 't', '',"
            "         'dev', 'user', 'main', 'feature', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "tid": ticket_id},
    )


@pytest.mark.asyncio
async def test_public_api_list_reviews_for_pr_callable(db_session) -> None:  # type: ignore[no-untyped-def]
    """`from app.domain import reviewer; reviewer.list_reviews_for_pr(pr_id, org_id=…)`
    must return a list of view objects with `id` + `sequence_number` set.
    """
    from app.domain import reviewer  # noqa: PLC0415

    assert hasattr(reviewer, "list_reviews_for_pr"), "reviewer.list_reviews_for_pr is part of the public API"
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr_id, org_id)
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, 1, 'posted', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )
    await db_session.commit()

    reviews = await reviewer.list_reviews_for_pr(pr_id, org_id=org_id)
    assert len(reviews) == 1
    assert reviews[0].sequence_number == 1


@pytest.mark.asyncio
async def test_public_api_get_review_callable(db_session) -> None:  # type: ignore[no-untyped-def]
    from app.domain import reviewer  # noqa: PLC0415

    assert hasattr(reviewer, "get_review")
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr_id, org_id)
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, 1, 'queued', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )
    await db_session.commit()

    r = await reviewer.get_review(review_id, org_id=org_id)
    assert r.id == review_id


@pytest.mark.asyncio
async def test_public_api_list_findings_for_pr_callable(db_session) -> None:  # type: ignore[no-untyped-def]
    from app.domain import reviewer  # noqa: PLC0415

    assert hasattr(reviewer, "list_findings_for_pr")
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    # No findings seeded — just confirm the call works on an empty PR.
    findings = await reviewer.list_findings_for_pr(pr_id, org_id=org_id)
    assert findings == []


@pytest.mark.asyncio
async def test_public_api_get_thread_callable(db_session) -> None:  # type: ignore[no-untyped-def]
    from app.domain import reviewer  # noqa: PLC0415

    assert hasattr(reviewer, "get_thread")
    # Unknown thread id returns None (not an exception).
    res = await reviewer.get_thread(uuid.uuid4(), org_id=uuid.uuid4())
    assert res is None


# ─── Domain events dispatch ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_aggregate_events_dispatched_to_sse_bus(db_session, redis_or_skip) -> None:  # type: ignore[no-untyped-def]
    """When the service layer saves an aggregate and commits, the aggregate's
    pending domain events are published to the org-scoped SSE channel so
    subscribers see `finding_raised` / `review_completed` etc.

    `dispatch_events` stashes events via `publish_general_after_commit`; the
    flush fires on `after_commit` — so the subscriber must be up before the
    commit.
    """
    import asyncio  # noqa: PLC0415

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.redis import reset_pubsub  # noqa: PLC0415
    from app.core.sse import subscribe_general  # noqa: PLC0415
    from app.domain.reviewer.aggregate import RawFinding  # noqa: PLC0415
    from app.domain.reviewer.repository import SqlAlchemyAggregateRepository  # noqa: PLC0415
    from app.domain.reviewer.service import dispatch_events  # noqa: PLC0415
    from app.domain.reviewer.types import (  # noqa: PLC0415
        CodeAnchor,
        FindingFingerprint,
    )

    reset_pubsub()
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    await _seed_pr(db_session, pr_id, org_id)
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, 1, 'queued', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )
    await db_session.commit()

    repo = SqlAlchemyAggregateRepository(db_session)
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    agg.post_process_raw_findings(
        review_id,
        [
            RawFinding(
                fingerprint=FindingFingerprint(
                    file_path="src/foo.py",
                    rule_id="r/x",
                    anchor_content_hash="anc",
                    body_gist_hash="gist",
                ),
                rule_id="r/x",
                title="t",
                body="b",
                rationale="r",
                concrete_failure_scenario="caller invokes f() without arg; raises TypeError.",
                confidence=90,
                severity="major",
                anchor=CodeAnchor(
                    file_path="src/foo.py",
                    line_start=1,
                    line_end=1,
                    surrounding_content_hash="surr",
                    commit_sha="abc",
                ),
                source_agent="test",
            )
        ],
    )

    received: list = []

    async def _consume() -> None:
        async for event in subscribe_general(org_id):
            received.append(event)
            if len(received) >= 1:
                return

    consumer = asyncio.create_task(_consume())
    # Tiny yield so the subscriber registers before publish fires.
    await asyncio.sleep(0.05)

    # Stash events; flush on commit.
    async with org_context(org_id, ActorKind.SYSTEM):
        dispatch_events(db_session, aggregate=agg)
    await db_session.commit()

    try:
        await asyncio.wait_for(consumer, timeout=2.0)
    except TimeoutError:
        consumer.cancel()

    kinds = {e["kind"] for e in received}
    assert kinds & {"finding_raised", "review_completed"}, (
        f"Expected at least one reviewer event kind on the SSE bus; got: {kinds}"
    )


# ─── Eval metrics ─────────────────────────────────────────────────────────────


def test_eval_metrics_module_exposes_acceptance_and_resolved_without_edit() -> None:
    """Three metrics exist: tier mix (already logged), acceptance_rate,
    resolved_without_edit_rate. The reviewer module must expose computation
    helpers for the latter two.
    """
    from app.domain import reviewer  # noqa: PLC0415

    assert hasattr(reviewer, "compute_acceptance_rate"), (
        "reviewer.compute_acceptance_rate is part of the eval metrics API"
    )
    assert hasattr(reviewer, "compute_resolved_without_edit_rate"), (
        "reviewer.compute_resolved_without_edit_rate is part of the eval metrics API"
    )
