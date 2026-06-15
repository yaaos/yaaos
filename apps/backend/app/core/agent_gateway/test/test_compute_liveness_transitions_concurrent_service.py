"""Service test: `compute_agent_liveness_transitions` holds row-level locks under
concurrent invocations so overlapping reaper sweeps cannot double-transition the
same agent.

Two concurrent callers via independent sessions race on the same N stale
`workspace_agents` rows. Because the SELECT carries `FOR UPDATE SKIP LOCKED`,
one caller wins each row; the other caller skips it. The union of both callers'
`newly_offline` lists contains exactly the N agent IDs — no duplicates.

Uses independent sessions off the live engine so the concurrent SELECTs truly
race on Postgres — the standard `db_session` fixture routes all writes through a
single connection, which prevents the race from materialising. All committed rows
are cleaned up in the fixture teardown so this test leaves no residue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.service import _OFFLINE_THRESHOLD_SECONDS, compute_agent_liveness_transitions
from app.core.database import get_sessionmaker

pytestmark = [pytest.mark.service, pytest.mark.asyncio]

# Number of stale agents to seed — large enough that both callers race on at
# least one row (Postgres distributes FOR UPDATE SKIP LOCKED picks across both
# connections nondeterministically).
_N = 6

# How far past the offline threshold to back-date heartbeats.
_HEARTBEAT_AGE = _OFFLINE_THRESHOLD_SECONDS + 60


async def _clean(agent_ids: list[UUID]) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await s.execute(delete(WorkspaceAgentRow).where(WorkspaceAgentRow.id.in_(agent_ids)))
        await s.commit()


@pytest_asyncio.fixture
async def _clean_agents() -> AsyncIterator[list[UUID]]:
    """Track agent row IDs committed via independent sessions; delete at teardown."""
    agent_ids: list[UUID] = []
    yield agent_ids
    await _clean(agent_ids)


async def _seed_stale_agents(n: int) -> list[UUID]:
    """Insert N workspace_agents rows with heartbeats past the offline threshold.

    Uses distinct org_ids per agent to avoid any org-level filtering concerns.
    Rows are committed via an independent session so concurrent callers in their
    own sessions can truly race on them.
    """
    stale_heartbeat_at = datetime.now(UTC) - timedelta(seconds=_HEARTBEAT_AGE)
    sessionmaker = get_sessionmaker()
    ids: list[UUID] = []
    async with sessionmaker() as s:
        for i in range(n):
            row = WorkspaceAgentRow(
                org_id=uuid4(),
                instance_id=f"test-concurrent-{uuid4().hex[:8]}",
                iam_arn=f"arn:aws:iam::123456789012:role/test-{i}",
                version="0.0.1",
                state="reachable",  # will transition to offline
                claimed_workspace_count=0,
                last_heartbeat_at=stale_heartbeat_at,
            )
            s.add(row)
            await s.flush()
            ids.append(row.id)
        await s.commit()
    return ids


async def _call_transitions() -> list[UUID]:
    """Open an independent session, call compute_agent_liveness_transitions, commit."""
    sessionmaker = get_sessionmaker()
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        newly_offline = await compute_agent_liveness_transitions(now, session=s)
        await s.commit()
    return newly_offline


async def test_compute_liveness_transitions_no_duplicate_offline_under_concurrency(
    _migrated_schema: None,
    _clean_agents: list[UUID],
) -> None:
    """Two concurrent compute_agent_liveness_transitions calls together report each
    agent as newly_offline exactly once — proving FOR UPDATE SKIP LOCKED prevents
    the same agent row from being transitioned by both callers."""
    agent_ids = await _seed_stale_agents(_N)
    _clean_agents.extend(agent_ids)

    offline_a, offline_b = await asyncio.gather(
        _call_transitions(),
        _call_transitions(),
    )

    union = set(offline_a) | set(offline_b)
    intersection = set(offline_a) & set(offline_b)

    assert union == set(agent_ids), (
        f"Expected newly_offline union == all {_N} agent IDs; union={len(union)} seeded={len(agent_ids)}"
    )
    assert not intersection, (
        f"Expected zero overlap (no agent transitioned twice); duplicate agent IDs: {intersection}"
    )
    assert len(offline_a) + len(offline_b) == _N, (
        f"Expected total newly_offline count == {_N} (one transition per agent); "
        f"got caller_a={len(offline_a)} caller_b={len(offline_b)}"
    )
