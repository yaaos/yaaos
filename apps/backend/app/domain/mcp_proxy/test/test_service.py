"""Coverage for `domain/mcp_proxy.service` — mint / lookup / revoke / sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.domain.identity import repository as identity_repo
from app.domain.mcp_proxy import lookup_token, mint_token, revoke_token, sweep_expired
from app.domain.mcp_proxy.models import McpReviewTokenRow
from app.domain.orgs import repository as orgs_repo


async def _seed_review(db_session) -> tuple:
    from app.domain.pull_requests import PullRequestRow  # noqa: PLC0415
    from app.domain.reviewer import ReviewRow  # noqa: PLC0415
    from app.domain.tickets import TicketRow  # noqa: PLC0415

    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="mcp-test")
    ticket = TicketRow(
        id=uuid4(),
        org_id=org.id,
        source="github_pr",
        source_external_id="pr-1",
        title="t",
        plugin_id="github",
        repo_external_id="owner/repo",
    )
    db_session.add(ticket)
    await db_session.flush()
    pr = PullRequestRow(
        id=uuid4(),
        org_id=org.id,
        plugin_id="github",
        repo_external_id="owner/repo",
        external_id="pr-1",
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
        ticket_id=ticket.id,
    )
    db_session.add(pr)
    await db_session.flush()
    review = ReviewRow(
        id=uuid4(),
        org_id=org.id,
        pr_id=pr.id,
        sequence_number=1,
        status="queued",
        trigger_reason="manual_full",
        destination="vcs",
    )
    db_session.add(review)
    await db_session.flush()
    return user, org, pr, review


@pytest.mark.asyncio
async def test_mint_returns_raw_token_persists_hash(db_session) -> None:
    _, _, _, review = await _seed_review(db_session)
    raw = await mint_token(review.id, session=db_session)
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
async def test_lookup_returns_row_for_valid_token(db_session) -> None:
    _, _, _, review = await _seed_review(db_session)
    raw = await mint_token(review.id, session=db_session)
    row = await lookup_token(raw, session=db_session)
    assert row is not None
    assert row.review_id == review.id


@pytest.mark.asyncio
async def test_lookup_returns_none_for_unknown(db_session) -> None:
    assert await lookup_token("never-issued", session=db_session) is None


@pytest.mark.asyncio
async def test_lookup_returns_none_for_expired(db_session) -> None:
    _, _, _, review = await _seed_review(db_session)
    raw = await mint_token(review.id, session=db_session)
    # Backdate so lookup_token sees it as expired.
    row = (
        await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review.id))
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()
    assert await lookup_token(raw, session=db_session) is None


@pytest.mark.asyncio
async def test_revoke_drops_all_rows_for_review(db_session) -> None:
    _, _, _, review = await _seed_review(db_session)
    await mint_token(review.id, session=db_session)
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

    _, _, _, review = await _seed_review(db_session)
    fresh = await mint_token(review.id, session=db_session)
    expired = await mint_token(review.id, session=db_session)
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
