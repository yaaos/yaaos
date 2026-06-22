"""Coverage for `domain/mcp_proxy.service` — mint / lookup / revoke / sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.identity import create_user
from app.core.vcs import VCSPullRequest
from app.domain.mcp_proxy import lookup_token, mint_token, revoke_token
from app.domain.mcp_proxy.models import McpReviewTokenRow
from app.domain.mcp_proxy.service import _sweep_once, sweep_expired
from app.domain.orgs import insert_org
from app.domain.reviewer import (
    PRReviewAggregate,
    Review,
    ReviewScope,
    ReviewTrigger,
    SqlAlchemyAggregateRepository,
)
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import upsert as upsert_pr


async def _seed_review(db_session) -> tuple:  # type: ignore[return]
    user = await create_user(db_session, display_name="U")
    org = await insert_org(db_session, slug=f"mcp-test-{uuid4().hex[:6]}")
    ext_id = "pr-1"
    idempotency_key = f"{ext_id}-{uuid4().hex[:6]}"
    ticket_id, _ = await create_ticket(
        org_id=org.org_id,
        source_external_id=ext_id,
        title="t",
        description=None,
        repo_external_id="owner/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
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
    review: Review = agg.start_review(
        trigger=ReviewTrigger.MANUAL_FULL,
        scope=ReviewScope.full(base_sha="0", head_sha="1"),
        commit_sha="1",
    )
    repo = SqlAlchemyAggregateRepository(db_session)
    await repo.save(agg)
    return user, org, pr, review


@pytest.mark.asyncio
async def test_mint_returns_raw_token_persists_hash(db_session) -> None:
    _, org, _, review = await _seed_review(db_session)
    raw = await mint_token(review.id, org_id=org.org_id, session=db_session)
    assert len(raw) > 32  # URL-safe base64 of 32 random bytes
    rows = (
        (await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # Raw token never stored — only sha256 hex.
    assert rows[0].token_hash != raw
    assert len(rows[0].token_hash) == 64


@pytest.mark.asyncio
async def test_mint_token_stores_org_id(db_session) -> None:
    """org_id is persisted on the token row so the proxy reads tenancy without a reviewer back-lookup."""
    _, org, _, review = await _seed_review(db_session)
    raw = await mint_token(review.id, org_id=org.org_id, session=db_session)
    rows = (
        (await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].org_id == org.org_id
    # Cross-check via lookup_token value object.
    token = await lookup_token(raw, session=db_session)
    assert token is not None
    assert token.org_id == org.org_id


@pytest.mark.asyncio
async def test_lookup_returns_row_for_valid_token(db_session) -> None:
    _, org, _, review = await _seed_review(db_session)
    raw = await mint_token(review.id, org_id=org.org_id, session=db_session)
    row = await lookup_token(raw, session=db_session)
    assert row is not None
    assert row.review_id == review.id


@pytest.mark.asyncio
async def test_lookup_returns_none_for_unknown(db_session) -> None:
    assert await lookup_token("never-issued", session=db_session) is None


@pytest.mark.asyncio
async def test_lookup_returns_none_for_expired(db_session) -> None:
    _, org, _, review = await _seed_review(db_session)
    raw = await mint_token(review.id, org_id=org.org_id, session=db_session)
    # Backdate so lookup_token sees it as expired.
    row = (
        await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review.id))
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()
    assert await lookup_token(raw, session=db_session) is None


@pytest.mark.asyncio
async def test_revoke_drops_all_rows_for_review(db_session) -> None:
    _, org, _, review = await _seed_review(db_session)
    await mint_token(review.id, org_id=org.org_id, session=db_session)
    n = await revoke_token(review.id, session=db_session)
    assert n == 1
    rows = (
        (await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review.id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_sweep_drops_expired_keeps_fresh(db_session) -> None:
    import hashlib  # noqa: PLC0415

    _, org, _, review = await _seed_review(db_session)
    fresh = await mint_token(review.id, org_id=org.org_id, session=db_session)
    expired = await mint_token(review.id, org_id=org.org_id, session=db_session)
    # Backdate the expired token by targeting its sha256 hash directly.
    expired_hash = hashlib.sha256(expired.encode()).hexdigest()
    row = (
        await db_session.execute(
            select(McpReviewTokenRow).where(McpReviewTokenRow.token_hash == expired_hash)
        )
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    n = await sweep_expired(session=db_session)
    assert n == 1
    assert await lookup_token(fresh, session=db_session) is not None
    assert await lookup_token(expired, session=db_session) is None


@pytest.mark.asyncio
async def test_mcp_proxy_sweep_deletes_expired(db_session) -> None:
    """_sweep_once deletes expired token rows."""
    import hashlib  # noqa: PLC0415

    _, org, _, review = await _seed_review(db_session)
    expired_raw = await mint_token(review.id, org_id=org.org_id, session=db_session)
    # Backdate via the same test session so it's visible to the sweep's own session.
    expired_hash = hashlib.sha256(expired_raw.encode()).hexdigest()
    row = (
        await db_session.execute(
            select(McpReviewTokenRow).where(McpReviewTokenRow.token_hash == expired_hash)
        )
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    await _sweep_once()

    # Expired row must be gone — verify via the test session.
    db_session.expire_all()
    gone = (
        await db_session.execute(
            select(McpReviewTokenRow).where(McpReviewTokenRow.token_hash == expired_hash)
        )
    ).scalar_one_or_none()
    assert gone is None
