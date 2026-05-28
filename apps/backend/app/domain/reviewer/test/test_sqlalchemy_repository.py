"""Integration tests for `SqlAlchemyAggregateRepository`.

Uses the `db_session` transactional fixture so writes land in a rolled-back
transaction. The repo's `load`/`save` is exercised end-to-end: build an
aggregate via the public API, save, reload, assert.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.domain.reviewer.aggregate import RawFinding
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.types import (
    CodeAnchor,
    FindingFingerprint,
    FindingState,
)


def _raw(rule_id: str = "test/null-deref", line: int = 10) -> RawFinding:
    return RawFinding(
        fingerprint=FindingFingerprint(
            file_path="src/foo.py",
            rule_id=rule_id,
            anchor_content_hash=f"anc-{rule_id}-{line}",
            body_gist_hash=f"gist-{rule_id}",
        ),
        rule_id=rule_id,
        title="x could be None",
        body="caller may pass None",
        rationale="raises NoneType error",
        concrete_failure_scenario="caller calls foo(None); .bar() raises.",
        confidence=90,
        severity="major",
        anchor=CodeAnchor(
            file_path="src/foo.py",
            line_start=line,
            line_end=line,
            surrounding_content_hash="surr",
            commit_sha="abc",
        ),
        source_agent="test",
    )


async def _seed_pr_and_review(db_session, pr_id: uuid.UUID, org_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Insert a minimal pull_requests row + a ReviewRow so FKs validate.

    Returns the ReviewRow id.
    """
    # Minimal pull_requests row — schema-driven INSERT via raw SQL keeps the
    # test focused on the reviewer repo's behavior. Columns mirror
    # app/domain/pull_requests/models.py PullRequestRow.
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets"
            " (id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', 'acme/web#1', 't',"
            "         'in_review', 'github', 'acme/web')"
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
    review_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status, trigger_reason, scope_kind, destination)"
            " VALUES (:id, :org_id, :pr_id, 1, 'queued', 'pr_ready', 'full', 'vcs')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )
    await db_session.commit()
    return review_id


@pytest.mark.asyncio
async def test_repository_round_trips_findings(db_session) -> None:  # type: ignore[no-untyped-def]
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = await _seed_pr_and_review(db_session, pr_id, org_id)

    # Save a finding via the aggregate.
    repo = SqlAlchemyAggregateRepository(db_session)
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    new_findings, _obs, _drops = agg.post_process_raw_findings(review_id, [_raw()])
    assert len(new_findings) == 1
    await repo.save(agg)
    await db_session.commit()

    # Reload — finding round-trips.
    agg2 = await repo.load(pr_id=pr_id, org_id=org_id)
    assert len(agg2.findings) == 1
    f = agg2.findings[0]
    assert f.rule_id == "test/null-deref"
    assert f.state == FindingState.OPEN
    assert f.fingerprint == new_findings[0].fingerprint


@pytest.mark.asyncio
async def test_repository_round_trips_thread_and_messages(db_session) -> None:  # type: ignore[no-untyped-def]
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = await _seed_pr_and_review(db_session, pr_id, org_id)

    repo = SqlAlchemyAggregateRepository(db_session)
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    new_findings, _, _ = agg.post_process_raw_findings(review_id, [_raw()])
    finding = new_findings[0]
    thread = agg.open_thread_for_finding(finding.id, external_thread_id="gh-review-thread-123")
    msg_yaaos = agg.append_message(
        thread_id=thread.id,
        author_kind="yaaos",
        author_external_id="yaaos",
        external_comment_id="gh-comment-1",
        body="yaaos finding body",
    )
    msg_human = agg.append_message(
        thread_id=thread.id,
        author_kind="human",
        author_external_id="dev1",
        external_comment_id="gh-comment-2",
        body="thanks",
        in_reply_to_external_id="gh-comment-1",
        classified_intent="other",
    )
    await repo.save(agg)
    await db_session.commit()

    agg2 = await repo.load(pr_id=pr_id, org_id=org_id)
    assert len(agg2.threads) == 1
    assert agg2.threads[0].external_thread_id == "gh-review-thread-123"
    messages = sorted(agg2.messages, key=lambda m: m.created_at)
    assert [m.external_comment_id for m in messages] == [
        "gh-comment-1",
        "gh-comment-2",
    ]
    assert messages[1].classified_intent == "other"
    # Suppress the unused-var warning — msg_yaaos / msg_human aren't asserted
    # against directly because we re-fetch via reload above.
    del msg_yaaos, msg_human


@pytest.mark.asyncio
async def test_repository_persists_review_writes(db_session) -> None:  # type: ignore[no-untyped-def]
    """`complete_review`/`mark_review_running`/etc. on the aggregate
    must reach the DB through the repository. Today this is a silent no-op
    (the SQLAlchemy save() skips reviews); this test pins the gap.
    """
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = await _seed_pr_and_review(db_session, pr_id, org_id)

    repo = SqlAlchemyAggregateRepository(db_session)
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    new_findings, _, _ = agg.post_process_raw_findings(review_id, [_raw()])
    # Aggregate transitions review state — repository should persist it.
    agg.complete_review(review_id, [f.id for f in new_findings])
    await repo.save(agg)
    await db_session.commit()

    status = (
        await db_session.execute(text("SELECT status FROM reviews WHERE id = :id"), {"id": review_id})
    ).scalar_one()
    assert status == "done"


@pytest.mark.asyncio
async def test_repository_persists_acknowledgment(db_session) -> None:  # type: ignore[no-untyped-def]
    pr_id, org_id = uuid.uuid4(), uuid.uuid4()
    review_id = await _seed_pr_and_review(db_session, pr_id, org_id)

    repo = SqlAlchemyAggregateRepository(db_session)
    agg = await repo.load(pr_id=pr_id, org_id=org_id)
    new_findings, _, _ = agg.post_process_raw_findings(review_id, [_raw()])
    finding = new_findings[0]
    thread = agg.open_thread_for_finding(finding.id)
    msg = agg.append_message(
        thread_id=thread.id,
        author_kind="human",
        author_external_id="dev1",
        external_comment_id="c1",
        body="by design",
    )
    agg.acknowledge(
        finding_id=finding.id,
        kind="intentional",
        rationale="by design",
        made_by_external_id="dev1",
        made_by_message_id=msg.id,
    )
    await repo.save(agg)
    await db_session.commit()

    agg2 = await repo.load(pr_id=pr_id, org_id=org_id)
    assert agg2.findings[0].state == FindingState.ACKNOWLEDGED
    assert len(agg2.threads) == 1
