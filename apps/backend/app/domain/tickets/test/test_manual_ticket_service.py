"""Service tests for manually-created tickets: create_from_manual + get_by_branch."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, list_for_entity
from app.core.tenancy import create_org
from app.domain.tickets.models import TicketRow
from app.domain.tickets.service import (
    Ticket,
    create_from_manual,
    get_by_branch,
    mint_branch_name,
)

pytestmark = pytest.mark.service


async def _seed_org(db_session: AsyncSession):
    return await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")


@pytest.mark.asyncio
async def test_create_from_manual_mints_branch_when_omitted(db_session: AsyncSession) -> None:
    """With no branch_name supplied, create_from_manual mints yaaos/<slug>-<shortid>."""
    org = await _seed_org(db_session)

    ticket_id, created = await create_from_manual(
        org_id=org.org_id,
        title="Refactor the login handler",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()

    assert created is True

    row = (await db_session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one()

    expected_branch = mint_branch_name("Refactor the login handler", ticket_id)
    assert row.branch_name == expected_branch
    assert row.status == "pending"
    assert row.type == "manual"
    assert row.source == "manual"
    assert row.plugin_id == ""


@pytest.mark.asyncio
async def test_create_from_manual_respects_caller_supplied_branch(db_session: AsyncSession) -> None:
    """When branch_name is supplied, create_from_manual uses it unchanged."""
    org = await _seed_org(db_session)

    ticket_id, created = await create_from_manual(
        org_id=org.org_id,
        title="Fix caching bug",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
        branch_name="my/custom-branch",
    )
    await db_session.commit()

    assert created is True

    row = (await db_session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one()

    assert row.branch_name == "my/custom-branch"


@pytest.mark.asyncio
async def test_create_from_manual_writes_ticket_created_audit(db_session: AsyncSession) -> None:
    """create_from_manual writes a ticket.created audit row on the winning insert."""
    org = await _seed_org(db_session)

    ticket_id, _ = await create_from_manual(
        org_id=org.org_id,
        title="Audit test task",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()

    entries = await list_for_entity("ticket", ticket_id, org_id=org.org_id)
    kinds = [e.kind for e in entries]
    assert "ticket.created" in kinds


@pytest.mark.asyncio
async def test_create_from_manual_idempotency_key_replay_returns_false(
    db_session: AsyncSession,
) -> None:
    """Supplying the same idempotency_key twice returns created=False on the second call."""
    org = await _seed_org(db_session)
    key = f"idem-{uuid4().hex}"

    first_id, first_created = await create_from_manual(
        org_id=org.org_id,
        title="Idempotent task",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
        idempotency_key=key,
    )
    await db_session.commit()

    second_id, second_created = await create_from_manual(
        org_id=org.org_id,
        title="Idempotent task (duplicate)",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
        idempotency_key=key,
    )
    await db_session.commit()

    assert first_created is True
    assert second_created is False
    assert first_id == second_id


@pytest.mark.asyncio
async def test_create_from_manual_no_key_creates_distinct_tickets(db_session: AsyncSession) -> None:
    """Without an idempotency_key, each call creates a distinct ticket."""
    org = await _seed_org(db_session)

    id_a, _ = await create_from_manual(
        org_id=org.org_id,
        title="Task A",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()

    id_b, _ = await create_from_manual(
        org_id=org.org_id,
        title="Task A",  # same title, no key → fresh ticket
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.commit()

    assert id_a != id_b


@pytest.mark.asyncio
async def test_get_by_branch_returns_newest_ticket(db_session: AsyncSession) -> None:
    """get_by_branch returns the most-recently-created ticket on the branch."""
    org = await _seed_org(db_session)

    _id_older, _ = await create_from_manual(
        org_id=org.org_id,
        title="Older task",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
        branch_name="yaaos/shared-branch",
    )
    await db_session.commit()

    id_newer, _ = await create_from_manual(
        org_id=org.org_id,
        title="Newer task",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
        branch_name="yaaos/shared-branch",
    )
    await db_session.commit()

    result = await get_by_branch("yaaos/shared-branch", org_id=org.org_id, session=db_session)

    assert result is not None
    assert isinstance(result, Ticket)
    assert result.id == id_newer


@pytest.mark.asyncio
async def test_get_by_branch_returns_none_when_not_found(db_session: AsyncSession) -> None:
    """get_by_branch returns None for an unknown branch."""
    org = await _seed_org(db_session)
    result = await get_by_branch("yaaos/nonexistent", org_id=org.org_id, session=db_session)
    assert result is None
