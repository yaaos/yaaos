"""Service test: `requeue_stale_claimed` holds row-level locks under concurrent
invocations so overlapping reaper sweeps cannot double-requeue the same row.

Two concurrent callers via independent sessions race on the same N stale-claimed
`agent_commands` rows. Because the SELECT carries `FOR UPDATE SKIP LOCKED`, one
caller wins each row; the other caller skips it. The total requeue count across
both callers equals N (not 2N) and each row's final `attempt` is incremented by
exactly 1.

Uses independent sessions off the live engine so the concurrent SELECTs truly
race on Postgres — the standard `db_session` fixture routes all writes through a
single connection, which prevents the race from materialising. All committed rows
are cleaned up in the fixture teardown so this test leaves no residue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import LEASE_SECONDS, requeue_stale_claimed
from app.core.database import get_sessionmaker

pytestmark = [pytest.mark.service, pytest.mark.asyncio]

# Number of stale-claimed rows to seed — large enough that both callers race
# on at least one row (Postgres distributes the FOR UPDATE SKIP LOCKED picks
# across both connections nondeterministically).
_N = 6


async def _clean(org_ids: list[UUID]) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await s.execute(delete(AgentCommandRow).where(AgentCommandRow.org_id.in_(org_ids)))
        await s.commit()


@pytest_asyncio.fixture
async def _clean_commands() -> AsyncIterator[list[UUID]]:
    """Track org_ids seeded via independent sessions; delete them at teardown."""
    org_ids: list[UUID] = []
    yield org_ids
    await _clean(org_ids)


async def _seed_stale_claimed_rows(org_id: UUID, n: int) -> list[UUID]:
    """Insert N stale-claimed agent_commands rows; return their IDs.

    Rows are committed via an independent session so subsequent concurrent
    callers in their own sessions can truly race on them.
    """
    stale_claimed_at = datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS + 10)
    sessionmaker = get_sessionmaker()
    ids: list[UUID] = []
    async with sessionmaker() as s:
        for _ in range(n):
            row = AgentCommandRow(
                id=uuid7(),
                org_id=org_id,
                command_kind="ProvisionWorkspace",
                payload={},
                status="claimed",
                agent_id=None,
                claimed_at=stale_claimed_at,
                attempt=0,
            )
            s.add(row)
            await s.flush()
            ids.append(row.id)
        await s.commit()
    return ids


async def _call_requeue() -> int:
    """Open an independent session, call requeue_stale_claimed, commit, return count."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        count = await requeue_stale_claimed(session=s)
        await s.commit()
    return count


async def test_requeue_stale_claimed_no_double_requeue_under_concurrency(
    _migrated_schema: None,
    _clean_commands: list[UUID],
) -> None:
    """Two concurrent requeue_stale_claimed calls together requeue exactly N rows
    (not 2N) and each row's attempt increments by exactly 1 — proving FOR UPDATE
    SKIP LOCKED prevents double-processing."""
    org_id = uuid4()
    _clean_commands.append(org_id)

    ids = await _seed_stale_claimed_rows(org_id, _N)

    count_a, count_b = await asyncio.gather(
        _call_requeue(),
        _call_requeue(),
    )

    total = count_a + count_b
    assert total == _N, (
        f"Expected total requeue count == {_N} across both callers, "
        f"got count_a={count_a} count_b={count_b} total={total}"
    )

    # Every row must have attempt=1 (incremented once, not twice).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        rows = (await s.execute(select(AgentCommandRow).where(AgentCommandRow.id.in_(ids)))).scalars().all()

    assert len(rows) == _N
    for row in rows:
        assert row.attempt == 1, (
            f"Row {row.id}: expected attempt=1 (incremented once), got attempt={row.attempt}"
        )
        assert row.status == "pending", (
            f"Row {row.id}: expected status='pending' after requeue, got '{row.status}'"
        )
