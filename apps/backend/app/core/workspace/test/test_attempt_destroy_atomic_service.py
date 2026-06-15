"""Service test: `_attempt_destroy` increments `destroy_attempts` at the SQL
layer so the cap holds under overlapping reaper sweeps.

Two concurrent `_attempt_destroy` calls on the same workspace row (with a
no-op provider) both increment; the final value is exactly 2, not 1.

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
from sqlalchemy import delete, select

from app.core.database import get_sessionmaker
from app.core.workspace import register_workspace_provider
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.service import _attempt_destroy
from app.core.workspace.types import WorkspaceStatus
from app.testing.seed import delete_workspace_agent, seed_agent

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


class _NoopProvider:
    """Provider whose destroy() is a no-op success — lets _attempt_destroy
    reach the full success path without side effects."""

    plugin_id = "noop-atomic"

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"sha": spec.sha}

    async def destroy(self) -> None:
        pass

    async def health_check(self) -> None:
        return None

    async def run_coding_agent_cli(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture(autouse=True)
def _register_noop(workspace_providers_isolation):
    del workspace_providers_isolation  # fixture handles clear before+after
    register_workspace_provider(_NoopProvider())


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


async def test_attempt_destroy_sql_increment_is_exact_under_concurrency(
    _migrated_schema: None,
    _seeded: _Seeded,
) -> None:
    """Two concurrent _attempt_destroy calls each increment destroy_attempts by 1
    at the SQL layer; the final value is exactly 2, proving no lost update."""
    org_id = uuid4()
    sessionmaker = get_sessionmaker()

    # Seed agent and workspace via independent sessions so subsequent concurrent
    # calls to _attempt_destroy (which open their own sessions) can truly race.
    async with sessionmaker() as s:
        agent = await seed_agent(org_id=org_id, session=s)
        agent_id = agent["id"]
        await s.commit()
    _seeded.agent_ids.append(agent_id)

    async with sessionmaker() as s:
        row = WorkspaceRow(
            id=uuid7(),
            org_id=org_id,
            provider_id="noop-atomic",
            spec={"sha": "deadbeef"},
            status=WorkspaceStatus.EXPIRED.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            destroy_attempts=0,
            owning_agent_id=agent_id,
        )
        s.add(row)
        await s.commit()
    _seeded.workspace_ids.append(row.id)

    await asyncio.gather(
        _attempt_destroy(row),
        _attempt_destroy(row),
    )

    async with sessionmaker() as s:
        refreshed = (await s.execute(select(WorkspaceRow).where(WorkspaceRow.id == row.id))).scalar_one()
    assert refreshed.destroy_attempts == 2, (
        f"Expected destroy_attempts=2 after two concurrent calls, got {refreshed.destroy_attempts}"
    )
