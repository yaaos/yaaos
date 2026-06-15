"""Service test: `ensure_agent_row` is idempotent under concurrent identity
exchanges for the same `(org_id, instance_id)`.

Two concurrent callers using independent sessions both see the same returned
UUID and exactly one `workspace_agents` row exists after both commits.

Uses independent sessions off the live engine (not the db_session fixture) so
the concurrent inserts actually race on the real Postgres unique constraint —
the standard `db_session` fixture wraps everything in a single connection so
concurrent writers would never truly race. Data is cleaned up in the fixture
so the test is self-contained and leaves no residue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.service import ensure_agent_row
from app.core.database import get_sessionmaker

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


async def _clean(org_ids: list[UUID]) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        await s.execute(delete(WorkspaceAgentRow).where(WorkspaceAgentRow.org_id.in_(org_ids)))
        await s.commit()


@pytest_asyncio.fixture
async def _clean_agents() -> AsyncIterator[list[UUID]]:
    org_ids: list[UUID] = []
    yield org_ids
    await _clean(org_ids)


async def _call_ensure(
    org_id: UUID,
    instance_id: str,
    iam_arn: str,
    version: str,
) -> UUID:
    """Open an independent session, call ensure_agent_row, commit, return the id."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        agent_id = await ensure_agent_row(
            org_id=org_id,
            instance_id=instance_id,
            iam_arn=iam_arn,
            version=version,
            session=s,
        )
        await s.commit()
    return agent_id


async def test_ensure_agent_row_concurrent_same_key(
    _migrated_schema: None,
    _clean_agents: list[UUID],
) -> None:
    """Two concurrent ensure_agent_row calls with the same (org_id, instance_id)
    both succeed, return the same UUID, and leave exactly one row in the DB."""
    org_id = uuid4()
    _clean_agents.append(org_id)
    instance_id = f"test-instance-{uuid4().hex[:8]}"
    iam_arn = "arn:aws:iam::123456789012:role/yaaos-agent"

    id_a, id_b = await asyncio.gather(
        _call_ensure(org_id, instance_id, iam_arn, "0.1.0"),
        _call_ensure(org_id, instance_id, iam_arn, "0.1.0"),
    )

    assert id_a == id_b, "Both callers must return the same agent row id"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    select(WorkspaceAgentRow).where(
                        WorkspaceAgentRow.org_id == org_id,
                        WorkspaceAgentRow.instance_id == instance_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"Expected exactly 1 workspace_agents row, found {len(rows)}"
    assert rows[0].id == id_a
