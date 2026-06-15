"""Service test: `close_workspace` is atomically idempotent under concurrent
admin requests and racy reaper transitions.

Two scenarios:
- Concurrent: two `close_workspace` calls on an ACTIVE workspace race; exactly
  one `workspace.transitioned` audit row is written, with from='active' and
  to='expired'.
- Already-expired: calling `close_workspace` on a workspace already in
  EXPIRED state is a no-op; no new audit row is written.

The concurrent scenario seeds via get_sessionmaker() (independent connections)
so the concurrent UPDATEs inside close_workspace truly race on Postgres — the
standard `db_session` fixture routes all writes through a single connection and
would prevent true racing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.audit_log import list_for_entity
from app.core.database import get_sessionmaker
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.service import close_workspace
from app.core.workspace.types import WorkspaceStatus
from app.testing.seed import delete_workspace_agent, seed_agent

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


@dataclass
class _Seeded:
    """Tracks rows committed via independent sessions for teardown cleanup."""

    workspace_ids: list[UUID] = field(default_factory=list)
    agent_ids: list[UUID] = field(default_factory=list)


async def _clean(seeded: _Seeded) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        # Workspaces first — owning_agent_id FK is ON DELETE RESTRICT.
        await s.execute(delete(WorkspaceRow).where(WorkspaceRow.id.in_(seeded.workspace_ids)))
        for agent_id in seeded.agent_ids:
            await delete_workspace_agent(agent_id, session=s)
        await s.commit()


@pytest_asyncio.fixture
async def _seeded() -> AsyncIterator[_Seeded]:
    seeded = _Seeded()
    yield seeded
    await _clean(seeded)


async def test_close_workspace_concurrent_writes_exactly_one_audit_row(
    _migrated_schema: None,
    _seeded: _Seeded,
) -> None:
    """Two concurrent `close_workspace` calls on the same ACTIVE workspace
    produce exactly one audit row with kind='workspace.transitioned'."""
    org_id = uuid4()
    sessionmaker = get_sessionmaker()

    # Seed agent via independent session so concurrent close_workspace calls
    # (which open their own sessions) truly race on Postgres.
    async with sessionmaker() as s:
        agent = await seed_agent(org_id=org_id, session=s)
        agent_id = agent["id"]
        await s.commit()
    _seeded.agent_ids.append(agent_id)

    async with sessionmaker() as s:
        row = WorkspaceRow(
            id=uuid7(),
            org_id=org_id,
            provider_id="remote_agent",
            spec={"sha": "deadbeef"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            owning_agent_id=agent_id,
        )
        s.add(row)
        await s.commit()
    _seeded.workspace_ids.append(row.id)

    await asyncio.gather(
        close_workspace(row.id),
        close_workspace(row.id),
    )

    async with sessionmaker() as s:
        refreshed = (await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.status == WorkspaceStatus.EXPIRED.value

    # Exactly one audit row for this workspace transition.
    entries = await list_for_entity(
        "workspace",
        row.id,
        org_id=org_id,
        kinds=["workspace.transitioned"],
    )
    transition_entries = [e for e in entries if e.payload.get("to_state") == "expired"]
    assert len(transition_entries) == 1, (
        f"Expected exactly 1 workspace.transitioned (to=expired) audit row, found {len(transition_entries)}"
    )
    assert transition_entries[0].payload.get("from_state") == "active"


async def test_close_workspace_already_expired_writes_no_audit_row(
    _migrated_schema: None,
    _seeded: _Seeded,
) -> None:
    """Calling `close_workspace` on a workspace that is already EXPIRED is a
    no-op — no new audit row is written."""
    org_id = uuid4()
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as s:
        agent = await seed_agent(org_id=org_id, session=s)
        agent_id = agent["id"]
        await s.commit()
    _seeded.agent_ids.append(agent_id)

    async with sessionmaker() as s:
        row = WorkspaceRow(
            id=uuid7(),
            org_id=org_id,
            provider_id="remote_agent",
            spec={"sha": "deadbeef"},
            # Already expired — simulates a completed TTL sweep or prior close.
            status=WorkspaceStatus.EXPIRED.value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            owning_agent_id=agent_id,
        )
        s.add(row)
        await s.commit()
    _seeded.workspace_ids.append(row.id)

    await close_workspace(row.id)

    entries = await list_for_entity(
        "workspace",
        row.id,
        org_id=org_id,
        kinds=["workspace.transitioned"],
    )
    assert len(entries) == 0, f"Expected 0 audit rows for already-expired workspace, found {len(entries)}"
