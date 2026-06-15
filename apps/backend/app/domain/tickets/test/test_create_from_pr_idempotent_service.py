"""Service test: concurrent `create_from_pr` calls are race-safe.

Two concurrent inserts with the same (org_id, source, source_external_id)
produce at most one TicketRow. The race loser returns the winner's id and
`created=False`. Both calls finish without raising.

Uses `get_sessionmaker()` (independent committed sessions) so the two
concurrent calls truly race on Postgres. The `db_session` rollback fixture
is deliberately NOT used here — the test cleans up committed rows in teardown.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.database import get_sessionmaker
from app.domain.tickets import create_from_pr
from app.domain.tickets.models import TicketRow
from app.testing.seed import delete_ticket

pytestmark = [pytest.mark.service, pytest.mark.asyncio]

_REPO_EXTERNAL_ID = "idempotent-org/repo"


async def _clean(ticket_ids: list[UUID]) -> None:
    sessionmaker = get_sessionmaker()
    for tid in ticket_ids:
        async with sessionmaker() as s:
            await delete_ticket(tid, session=s)
            await s.commit()


@pytest_asyncio.fixture
async def _tracked_tickets() -> AsyncIterator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    await _clean(ids)


@pytest.mark.service
async def test_concurrent_create_from_pr_produces_one_row(
    _migrated_schema: None,
    _tracked_tickets: list[UUID],
) -> None:
    """Two concurrent `create_from_pr` calls with the same (org_id, source,
    source_external_id) produce exactly one TicketRow. The race loser returns
    the winner's id with `created=False`.
    """
    org_id = uuid4()
    source_external_id = f"race-{uuid4().hex[:8]}"
    idempotency_base = f"delivery-{uuid4().hex}"

    async def _call(suffix: str) -> tuple[UUID, bool]:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as s:
            result = await create_from_pr(
                org_id=org_id,
                source_external_id=source_external_id,
                title="concurrent PR",
                description=None,
                repo_external_id=_REPO_EXTERNAL_ID,
                plugin_id="github",
                idempotency_key=f"{idempotency_base}-{suffix}",
                payload={},
                session=s,
            )
            await s.commit()
        return result

    results = await asyncio.gather(_call("a"), _call("b"))
    ticket_id_a, created_a = results[0]
    ticket_id_b, created_b = results[1]

    # Exactly one creation; both return the same id.
    assert ticket_id_a == ticket_id_b, (
        f"Race loser must return winner's id; got {ticket_id_a!r} vs {ticket_id_b!r}"
    )
    assert (created_a, created_b).count(True) == 1, (
        f"Exactly one call must have created=True; got ({created_a}, {created_b})"
    )

    _tracked_tickets.append(ticket_id_a)

    # Exactly one TicketRow exists.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    select(TicketRow).where(
                        TicketRow.org_id == org_id,
                        TicketRow.source_external_id == source_external_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"Expected exactly 1 TicketRow; found {len(rows)}"
    assert rows[0].status == "pending"
