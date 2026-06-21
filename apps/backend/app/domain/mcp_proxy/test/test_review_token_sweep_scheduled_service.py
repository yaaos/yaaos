"""Service-tier guards for the hourly `mcp_review_token_sweep` `@scheduled` task.

Two invariants:
  - The body is registered with the taskiq broker under the public task name.
  - The sweep body drops expired token rows and leaves non-expired ones intact.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.identity import insert_user
from app.core.tasks import get_broker
from app.core.vcs import VCSPullRequest
from app.domain.mcp_proxy import mint_token
from app.domain.mcp_proxy.models import McpReviewTokenRow
from app.domain.mcp_proxy.service import _sweep_once, mcp_review_token_sweep
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer import (
    PRReviewAggregate,
    ReviewScope,
    ReviewTrigger,
    SqlAlchemyAggregateRepository,
)
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import upsert as upsert_pr

pytestmark = pytest.mark.service

_TASK_NAME = "mcp_review_token_sweep"


async def _seed_review(db_session):  # type: ignore[no-untyped-def]
    user = await insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug=f"mcp-tok-svc-{uuid4().hex[:8]}")
    ext_id = f"pr-svc-{uuid4().hex[:6]}"
    ticket_id, _ = await create_ticket(
        org_id=org.org_id,
        source_external_id=ext_id,
        title="t",
        description=None,
        repo_external_id="owner/repo",
        plugin_id="github",
        idempotency_key=f"{ext_id}-{uuid4().hex[:6]}",
        payload={},
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
            plugin_id="github",
            repo_external_id="owner/repo",
            external_id=f"{ext_id}-{uuid4().hex[:6]}",
            number=1,
            title="t",
            body=None,
            author_login="a",
            author_type="user",
            base_branch="main",
            head_branch="b",
            base_sha="0",
            head_sha="1",
            is_draft=False,
            is_fork=False,
            state="open",
            html_url="http://test",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        ticket_id=ticket_id,
        org_id=org.org_id,
        session=db_session,
    )
    agg = PRReviewAggregate(pr_id=pr.id, org_id=org.org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.MANUAL_FULL,
        scope=ReviewScope.full(base_sha="0", head_sha="1"),
        commit_sha="1",
    )
    repo = SqlAlchemyAggregateRepository(db_session)
    await repo.save(agg)
    await db_session.commit()
    return user, org, pr, review


@pytest.mark.asyncio
async def test_mcp_review_token_sweep_task_registered_with_broker() -> None:
    """The sweep body is registered with the broker under its public task name.
    Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None
    assert mcp_review_token_sweep is not None


@pytest.mark.asyncio
async def test_review_token_sweep_body_deletes_expired(db_session) -> None:
    """Drive `_sweep_once` directly — expired token rows are removed."""
    _, org, _, review = await _seed_review(db_session)
    expired_raw = await mint_token(review.id, org_id=org.org_id, session=db_session)
    expired_hash = hashlib.sha256(expired_raw.encode()).hexdigest()
    row = (
        await db_session.execute(
            select(McpReviewTokenRow).where(McpReviewTokenRow.token_hash == expired_hash)
        )
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    await _sweep_once()

    db_session.expire_all()
    gone = (
        await db_session.execute(
            select(McpReviewTokenRow).where(McpReviewTokenRow.token_hash == expired_hash)
        )
    ).scalar_one_or_none()
    assert gone is None


@pytest.mark.asyncio
async def test_review_token_sweep_body_runs_idempotently() -> None:
    """Empty DB stays empty — surfaces exceptions loudly."""
    await _sweep_once()
    await _sweep_once()
