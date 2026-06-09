"""Service-level tests for the PR mirror public ops (now owned by `domain/tickets`).

Covers list_by_ids — the batch read added so callers can enumerate multiple
PRs in one call without re-importing PullRequestRow.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.vcs import VCSPullRequest
from app.domain import tickets
from app.domain.tickets import create as create_ticket


def _vcs_pr(*, external_id: str, number: int = 1) -> VCSPullRequest:
    return VCSPullRequest(
        plugin_id="github",
        external_id=external_id,
        repo_external_id="org/repo",
        number=number,
        title=f"PR {external_id}",
        body=None,
        author_login="dev",
        author_type="user",
        base_branch="main",
        head_branch="feature",
        base_sha="a" * 40,
        head_sha="b" * 40,
        is_draft=False,
        is_fork=False,
        state="open",
        html_url=f"https://example.test/pr/{number}",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


async def _insert_pr(
    db_session,
    *,
    org_id,
    external_id: str,
    number: int = 1,
) -> tickets.PullRequest:
    """Helper: insert a ticket + PR row and return the PullRequest."""
    ticket_id, _ = await create_ticket(
        type="pr_review",
        payload={},
        idempotency_key=external_id,
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id=external_id,
        plugin_id="github",
        repo_external_id="org/repo",
        session=db_session,
    )
    pr = await tickets.upsert(
        _vcs_pr(external_id=external_id, number=number),
        ticket_id=ticket_id,
        org_id=org_id,
        session=db_session,
    )
    await db_session.commit()
    return pr


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_by_ids_returns_matching_prs(db_session) -> None:
    """list_by_ids returns PullRequest objects for all requested ids."""
    org_id = uuid4()
    pr_a = await _insert_pr(db_session, org_id=org_id, external_id="org/repo#10", number=10)
    pr_b = await _insert_pr(db_session, org_id=org_id, external_id="org/repo#11", number=11)

    result = await tickets.list_by_ids([pr_a.id, pr_b.id])

    result_ids = {p.id for p in result}
    assert pr_a.id in result_ids
    assert pr_b.id in result_ids
    assert len(result) == 2


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_by_ids_empty_input_returns_empty(db_session) -> None:
    """list_by_ids with an empty list returns an empty list without hitting the DB."""
    result = await tickets.list_by_ids([])
    assert result == []


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_by_ids_unknown_ids_omitted(db_session) -> None:
    """list_by_ids silently omits ids that do not exist."""
    result = await tickets.list_by_ids([uuid4(), uuid4()])
    assert result == []


@pytest.mark.asyncio
@pytest.mark.service
async def test_list_by_ids_partial_match(db_session) -> None:
    """list_by_ids returns only the ids that exist; missing ids are silently dropped."""
    org_id = uuid4()
    pr = await _insert_pr(db_session, org_id=org_id, external_id="org/repo#20", number=20)
    missing_id = uuid4()

    result = await tickets.list_by_ids([pr.id, missing_id])

    assert len(result) == 1
    assert result[0].id == pr.id
