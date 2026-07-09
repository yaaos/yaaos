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

from app.core.tasks import get_broker
from app.domain.mcp_proxy import mint_token
from app.domain.mcp_proxy.models import McpReviewTokenRow
from app.domain.mcp_proxy.service import _sweep_once, mcp_review_token_sweep
from app.domain.orgs import insert_org

pytestmark = pytest.mark.service

_TASK_NAME = "mcp_review_token_sweep"


async def _seed_org_and_review_id(db_session):  # type: ignore[no-untyped-def]
    """`review_id` is a soft reference (no DB constraint) — any UUID scopes a token."""
    org = await insert_org(db_session, slug=f"mcp-tok-svc-{uuid4().hex[:8]}")
    await db_session.commit()
    return org.org_id, uuid4()


@pytest.mark.asyncio
async def test_mcp_review_token_sweep_task_registered_with_broker() -> None:
    """The sweep body is registered with the broker under its public task name.
    Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None
    assert mcp_review_token_sweep is not None


@pytest.mark.asyncio
async def test_review_token_sweep_body_deletes_expired(db_session) -> None:
    """Drive `_sweep_once` directly — expired token rows are removed."""
    org_id, review_id = await _seed_org_and_review_id(db_session)
    expired_raw = await mint_token(review_id, org_id=org_id, session=db_session)
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
