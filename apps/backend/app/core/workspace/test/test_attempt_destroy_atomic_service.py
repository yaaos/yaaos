"""Service test: `_attempt_destroy` EXPIRED→DESTROYING transition is single-flight.

Two concurrent `_attempt_destroy` calls on the same EXPIRED workspace row must
not both call `provider.destroy()`. The status-narrowing WHERE clause
(WHERE status='expired') means only one caller wins the UPDATE; the other sees
rowcount==0 and returns immediately. Exactly one DESTROYING audit row, one
`provider.destroy()` call, and `destroy_attempts==1` after both settle.

Uses independent sessions off the live engine for seeding and verification so
the concurrent UPDATEs inside _attempt_destroy truly race on Postgres — the
standard `db_session` fixture routes all writes through a single connection,
which would prevent the race from materialising.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text

from app.core.audit_log import list_for_entity
from app.core.database import get_sessionmaker
from app.core.workspace import register_workspace_provider
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.service import _attempt_destroy
from app.core.workspace.types import WorkspaceStatus
from app.testing.e2e_setup import seed_agent

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


class _CountingProvider:
    """Provider that counts destroy() calls under a lock — lets the test assert
    exactly one provider.destroy() was called despite two concurrent attempts."""

    plugin_id = "counting-atomic"

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.destroy_count = 0

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self) -> None:
        async with self._lock:
            self.destroy_count += 1

    async def health_check(self) -> None:
        return None

    async def run_coding_agent_cli(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def _provider() -> _CountingProvider:
    return _CountingProvider()


@pytest.fixture(autouse=True)
def _register_counting(workspace_providers_isolation, _provider: _CountingProvider):
    del workspace_providers_isolation  # fixture handles clear before+after
    register_workspace_provider(_provider)


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
            await s.execute(text("DELETE FROM workspace_agents WHERE id = :id"), {"id": agent_id})
        await s.commit()


@pytest_asyncio.fixture
async def _seeded() -> AsyncIterator[_Seeded]:
    seeded = _Seeded()
    yield seeded
    await _clean(seeded)


async def test_attempt_destroy_single_flight_under_concurrency(
    _migrated_schema: None,
    _seeded: _Seeded,
    _provider: _CountingProvider,
) -> None:
    """Two concurrent _attempt_destroy calls on the same EXPIRED row:
    exactly one wins the EXPIRED→DESTROYING transition; the other exits early.
    Result: destroy_attempts==1, one DESTROYING audit row, one provider.destroy() call."""
    org_id = uuid4()
    sessionmaker = get_sessionmaker()

    agent = await seed_agent(org_id=org_id)
    agent_id = agent["id"]
    _seeded.agent_ids.append(agent_id)

    workspace_id = uuid7()
    async with sessionmaker() as s:
        row = WorkspaceRow(
            id=workspace_id,
            org_id=org_id,
            provider_id="counting-atomic",
            spec={"sha": "deadbeef"},
            status=WorkspaceStatus.EXPIRED.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            destroy_attempts=0,
            owning_agent_id=agent_id,
        )
        s.add(row)
        await s.commit()
    _seeded.workspace_ids.append(workspace_id)

    await asyncio.gather(
        _attempt_destroy(row),
        _attempt_destroy(row),
    )

    async with sessionmaker() as s:
        refreshed = (
            await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        ).scalar_one()

    # Single-flight: only one caller transitions EXPIRED→DESTROYING.
    assert refreshed.destroy_attempts == 1, (
        f"Expected destroy_attempts=1 (single-flight), got {refreshed.destroy_attempts}"
    )

    # Exactly one provider.destroy() call — the losing caller short-circuited.
    assert _provider.destroy_count == 1, (
        f"Expected exactly 1 provider.destroy() call, got {_provider.destroy_count}"
    )

    # Exactly one DESTROYING audit row.
    audit_entries = await list_for_entity("workspace", workspace_id, org_id=org_id)
    destroying_entries = [
        e
        for e in audit_entries
        if e.kind == "workspace.transitioned"
        and e.payload.get("to_state") == WorkspaceStatus.DESTROYING.value
    ]
    assert len(destroying_entries) == 1, (
        f"Expected exactly 1 DESTROYING audit row, got {len(destroying_entries)}"
    )
