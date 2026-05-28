"""`pull_requests.upsert` honors the required-session contract.

Regression: `upsert` must not open its own `db_session()` internally.
Doing so meant the github intake's PR-opened path inserted a `pull_requests`
row in a separate transaction from the freshly-inserted (not-yet-committed)
ticket. The FK on `pull_requests.ticket_id` fired before commit and the
whole webhook 500'd. The fix is the required-session pattern from
`apps/backend/docs/patterns.md` § Session management + atomicity — caller
owns the transaction so ticket + PR + audit land atomically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.domain import pull_requests
from app.domain.pull_requests import PullRequestRow
from app.domain.tickets import TicketRow
from app.domain.vcs import VCSPullRequest


def _vcs_pr(*, external_id: str = "acme/repo#42") -> VCSPullRequest:
    return VCSPullRequest(
        plugin_id="github",
        external_id=external_id,
        repo_external_id="acme/repo",
        number=42,
        title="add lookup",
        body="body",
        author_login="dev",
        author_type="user",
        base_branch="main",
        head_branch="feature",
        base_sha="b" * 40,
        head_sha="h" * 40,
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="https://example.test/pr/42",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_upsert_insert_uses_caller_session_and_satisfies_fk(db_session) -> None:
    """Insert path: a brand-new ticket and a brand-new PR row land in the
    same transaction. The FK on `pull_requests.ticket_id` resolves against
    the same-session pending ticket — no cross-session leak."""
    org_id = uuid4()
    ticket_id = uuid4()

    # Stage an unflushed ticket on the caller's session.
    db_session.add(
        TicketRow(
            id=ticket_id,
            org_id=org_id,
            source="github_pr",
            source_external_id="acme/repo#42",
            title="t",
            description=None,
            status="running",
            plugin_id="github",
            repo_external_id="acme/repo",
            type="github_pr",
        )
    )

    # Upsert the PR on the same session. If the service opened its own
    # transaction we would FK-violate here because the ticket is uncommitted.
    result = await pull_requests.upsert(_vcs_pr(), ticket_id=ticket_id, org_id=org_id, session=db_session)
    assert result.ticket_id == ticket_id

    # Row is visible on the caller's session (flushed, not committed).
    row = (
        await db_session.execute(select(PullRequestRow).where(PullRequestRow.id == result.id))
    ).scalar_one()
    assert row.ticket_id == ticket_id
    assert row.external_id == "acme/repo#42"


@pytest.mark.asyncio
async def test_upsert_update_path_writes_changed_fields_and_audit(db_session) -> None:
    """Update path: existing PR row, mutable fields refresh, immutable
    fields preserved. Caller session still owns the commit."""
    org_id = uuid4()
    ticket_id = uuid4()
    db_session.add(
        TicketRow(
            id=ticket_id,
            org_id=org_id,
            source="github_pr",
            source_external_id="acme/repo#99",
            title="t",
            description=None,
            status="running",
            plugin_id="github",
            repo_external_id="acme/repo",
            type="github_pr",
        )
    )
    initial = await pull_requests.upsert(
        _vcs_pr(external_id="acme/repo#99"),
        ticket_id=ticket_id,
        org_id=org_id,
        session=db_session,
    )

    # Mutate head sha + title and re-upsert without a ticket_id — update path.
    refreshed_vcs = _vcs_pr(external_id="acme/repo#99")
    refreshed_vcs.head_sha = "c" * 40
    refreshed_vcs.title = "renamed"
    refreshed = await pull_requests.upsert(refreshed_vcs, ticket_id=None, org_id=org_id, session=db_session)

    assert refreshed.id == initial.id
    assert refreshed.head_sha == "c" * 40
    assert refreshed.title == "renamed"
    # Immutable: ticket_id and external_id stick.
    assert refreshed.ticket_id == ticket_id
    assert refreshed.external_id == "acme/repo#99"


@pytest.mark.asyncio
async def test_upsert_insert_without_ticket_id_raises(db_session) -> None:
    """Insert path requires `ticket_id` — orphan PR rows have no meaning."""
    with pytest.raises(ValueError, match="ticket_id required"):
        await pull_requests.upsert(
            _vcs_pr(external_id="acme/repo#1"),
            ticket_id=None,
            org_id=uuid4(),
            session=db_session,
        )
