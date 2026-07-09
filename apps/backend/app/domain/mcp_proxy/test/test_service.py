"""Coverage for `domain/mcp_proxy.service` — mint / lookup / revoke / sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.domain.mcp_proxy import lookup_token, mint_token, revoke_token
from app.domain.mcp_proxy.models import McpReviewTokenRow
from app.domain.mcp_proxy.service import _sweep_once, sweep_expired
from app.domain.orgs import insert_org


async def _seed_org_and_review_id(db_session) -> tuple[UUID, UUID]:
    """`review_id` is a soft reference (no DB constraint) — any UUID scopes
    a token. Returns `(org_id, review_id)`."""
    org = await insert_org(db_session, slug=f"mcp-test-{uuid4().hex[:6]}")
    return org.org_id, uuid4()


@pytest.mark.asyncio
async def test_mint_returns_raw_token_persists_hash(db_session) -> None:
    org_id, review_id = await _seed_org_and_review_id(db_session)
    raw = await mint_token(review_id, org_id=org_id, session=db_session)
    assert len(raw) > 32  # URL-safe base64 of 32 random bytes
    rows = (
        (await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # Raw token never stored — only sha256 hex.
    assert rows[0].token_hash != raw
    assert len(rows[0].token_hash) == 64


@pytest.mark.asyncio
async def test_mint_token_stores_org_id(db_session) -> None:
    """org_id is persisted on the token row so the proxy reads tenancy without a back-lookup."""
    org_id, review_id = await _seed_org_and_review_id(db_session)
    raw = await mint_token(review_id, org_id=org_id, session=db_session)
    rows = (
        (await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review_id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].org_id == org_id
    # Cross-check via lookup_token value object.
    token = await lookup_token(raw, session=db_session)
    assert token is not None
    assert token.org_id == org_id


@pytest.mark.asyncio
async def test_lookup_returns_row_for_valid_token(db_session) -> None:
    org_id, review_id = await _seed_org_and_review_id(db_session)
    raw = await mint_token(review_id, org_id=org_id, session=db_session)
    row = await lookup_token(raw, session=db_session)
    assert row is not None
    assert row.review_id == review_id


@pytest.mark.asyncio
async def test_lookup_returns_none_for_unknown(db_session) -> None:
    assert await lookup_token("never-issued", session=db_session) is None


@pytest.mark.asyncio
async def test_lookup_returns_none_for_expired(db_session) -> None:
    org_id, review_id = await _seed_org_and_review_id(db_session)
    raw = await mint_token(review_id, org_id=org_id, session=db_session)
    # Backdate so lookup_token sees it as expired.
    row = (
        await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review_id))
    ).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()
    assert await lookup_token(raw, session=db_session) is None


@pytest.mark.asyncio
async def test_revoke_drops_all_rows_for_review(db_session) -> None:
    org_id, review_id = await _seed_org_and_review_id(db_session)
    await mint_token(review_id, org_id=org_id, session=db_session)
    n = await revoke_token(review_id, session=db_session)
    assert n == 1
    rows = (
        (await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review_id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_sweep_drops_expired_keeps_fresh(db_session) -> None:
    import hashlib  # noqa: PLC0415

    org_id, review_id = await _seed_org_and_review_id(db_session)
    fresh = await mint_token(review_id, org_id=org_id, session=db_session)
    expired = await mint_token(review_id, org_id=org_id, session=db_session)
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

    org_id, review_id = await _seed_org_and_review_id(db_session)
    expired_raw = await mint_token(review_id, org_id=org_id, session=db_session)
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
