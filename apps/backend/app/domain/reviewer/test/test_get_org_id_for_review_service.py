"""DB round-trip tests for `reviewer.get_org_id_for_review`."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from app.domain.reviewer.service import get_org_id_for_review


async def _seed_review(db_session, *, org_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Insert the minimal rows needed for a reviews row (ticket → pull_request → review)."""
    ticket_id = uuid.uuid4()
    pr_id = uuid.uuid4()
    review_id = uuid.uuid4()

    await db_session.execute(
        text(
            "INSERT INTO tickets"
            " (id, org_id, source, source_external_id, title, status, plugin_id, repo_external_id)"
            " VALUES (:id, :org_id, 'github_pr', :ext, 't', 'running', 'github', 'a/b')"
        ),
        {"id": ticket_id, "org_id": org_id, "ext": f"a/b#{uuid.uuid4().hex[:6]}"},
    )
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, ticket_id, plugin_id, external_id, repo_external_id, number, title,"
            "  body, author_login, author_type, base_branch, head_branch, base_sha, head_sha,"
            "  is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :tid, 'github', 'a/b#1', 'a/b', 1, 't', '',"
            "         'dev', 'user', 'main', 'feat', 'b', 'h', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "tid": ticket_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO reviews (id, org_id, pr_id, sequence_number, status)"
            " VALUES (:id, :org_id, :pr_id, 1, 'queued')"
        ),
        {"id": review_id, "org_id": org_id, "pr_id": pr_id},
    )
    await db_session.commit()
    return review_id


async def test_get_org_id_for_review_returns_org_id(db_session) -> None:  # type: ignore[no-untyped-def]
    """Happy path: returns org_id for an existing review row."""
    org_id = uuid.uuid4()
    review_id = await _seed_review(db_session, org_id=org_id)

    result = await get_org_id_for_review(review_id)
    assert result == org_id


async def test_get_org_id_for_review_returns_none_when_missing(db_session) -> None:  # type: ignore[no-untyped-def]
    """Unknown review_id returns None."""
    result = await get_org_id_for_review(uuid.uuid4())
    assert result is None
