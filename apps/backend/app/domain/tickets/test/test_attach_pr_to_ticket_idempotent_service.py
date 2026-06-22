"""Service test: concurrent `attach_pr_to_ticket` calls are race-safe.

The `WHERE pr_id IS NULL` guard on the UPDATE makes attach idempotent:
two concurrent calls for the same ticket produce exactly one `ticket.pr_bound`
audit row; the second call silently no-ops.

Uses `get_sessionmaker()` (independent committed sessions) so the two
concurrent calls truly race on Postgres. The `db_session` rollback fixture
is deliberately NOT used here — the test cleans up committed rows in teardown.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.audit_log import list_for_entity
from app.core.database import get_sessionmaker
from app.core.vcs import VCSPullRequest
from app.domain.tickets import attach_pr_to_ticket, create_from_pr
from app.domain.tickets import upsert as upsert_pr
from app.domain.tickets.models import TicketRow
from app.domain.tickets.pull_request import PullRequestRow

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


async def _clean(ticket_id: UUID | None, pr_id: UUID | None) -> None:
    sessionmaker = get_sessionmaker()
    # pull_requests.ticket_id → tickets.id (NOT NULL); delete PR first.
    async with sessionmaker() as s:
        if pr_id is not None:
            await s.execute(delete(PullRequestRow).where(PullRequestRow.id == pr_id))
        await s.commit()
    async with sessionmaker() as s:
        if ticket_id is not None:
            await s.execute(delete(TicketRow).where(TicketRow.id == ticket_id))
        await s.commit()


@pytest_asyncio.fixture
async def _seeded_ids() -> AsyncIterator[dict]:
    ids: dict = {"ticket_id": None, "pr_id": None}
    yield ids
    await _clean(ids["ticket_id"], ids["pr_id"])


@pytest.mark.service
async def test_concurrent_attach_produces_one_audit_row(
    _migrated_schema: None,
    _seeded_ids: dict,
) -> None:
    """Two concurrent `attach_pr_to_ticket` calls for the same ticket produce
    exactly one `ticket.pr_bound` audit row; the second call is a safe no-op.
    """
    org_id = uuid4()
    ext_id = f"attach-race-{uuid4().hex[:8]}"
    sessionmaker = get_sessionmaker()

    # Seed ticket and PR in one committed session.
    async with sessionmaker() as s:
        ticket_id, _ = await create_from_pr(
            org_id=org_id,
            source_external_id=ext_id,
            title="attach race test",
            description=None,
            repo_external_id="race-org/repo",
            plugin_id="github",
            idempotency_key=f"delivery-{uuid4().hex}",
            payload={},
            session=s,
        )
        now = datetime.now(UTC)
        pr = await upsert_pr(
            VCSPullRequest(
                plugin_id="github",
                repo_external_id="race-org/repo",
                external_id=ext_id,
                number=7,
                title="race PR",
                body=None,
                author_login="dev",
                author_type="user",
                base_branch="main",
                head_branch="feat",
                base_sha="aaa",
                head_sha="bbb",
                is_draft=False,
                is_fork=False,
                state="open",
                html_url="https://github.com/race-org/repo/pull/7",
                created_at=now,
                updated_at=now,
            ),
            ticket_id=ticket_id,
            org_id=org_id,
            session=s,
        )
        await s.commit()

    _seeded_ids["ticket_id"] = ticket_id
    _seeded_ids["pr_id"] = pr.id

    # Two concurrent attach calls.
    async def _attach() -> None:
        async with sessionmaker() as s:
            await attach_pr_to_ticket(ticket_id, org_id=org_id, pr_id=pr.id, session=s)
            await s.commit()

    await asyncio.gather(_attach(), _attach())

    # Exactly one ticket.pr_bound audit row must exist.
    rows = await list_for_entity(
        "ticket",
        ticket_id,
        org_id=org_id,
        kinds=["ticket.pr_bound"],
    )
    assert len(rows) == 1, f"Expected exactly 1 ticket.pr_bound audit row; found {len(rows)}"
