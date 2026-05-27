"""Service-level tests for ticket lifecycle ops.

Covers: upsert_ticket_for_pr (idempotency + race-safe insert),
attach_pr_to_ticket (pr_id IS NULL guard), and set_workflow_execution.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.tickets import (
    attach_pr_to_ticket,
    set_workflow_execution,
    upsert_ticket_for_pr,
)
from app.domain.tickets.service import get


@pytest.mark.service
async def test_upsert_ticket_for_pr_creates_new(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"

    ticket_id, created = await upsert_ticket_for_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="My PR title",
        description="desc",
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={"head_sha": "abc123"},
        session=db_session,
    )
    await db_session.commit()

    assert created is True
    assert ticket_id is not None

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.title == "My PR title"
    assert ticket.status == "running"
    assert ticket.pr_id is None


@pytest.mark.service
async def test_upsert_ticket_for_pr_idempotent(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"

    _ticket_id, created = await upsert_ticket_for_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR title",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert created is True

    # Second call with same (org, source, source_external_id) returns None
    ticket_id2, created2 = await upsert_ticket_for_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="Different title",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=f"delivery-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.commit()

    # Loser of the race: created=False, id=None
    assert created2 is False
    assert ticket_id2 is None


@pytest.mark.service
async def test_attach_pr_to_ticket_when_pr_id_is_null(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"
    pr_id = uuid4()

    ticket_id, _ = await upsert_ticket_for_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None

    await attach_pr_to_ticket(ticket_id, pr_id=pr_id, session=db_session)
    await db_session.commit()

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.pr_id == pr_id


@pytest.mark.service
async def test_attach_pr_to_ticket_no_op_when_already_set(db_session) -> None:  # type: ignore[no-untyped-def]
    """attach_pr_to_ticket WHERE pr_id IS NULL: must not overwrite an already-set pr_id."""
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"
    first_pr_id = uuid4()
    second_pr_id = uuid4()

    ticket_id, _ = await upsert_ticket_for_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None

    await attach_pr_to_ticket(ticket_id, pr_id=first_pr_id, session=db_session)
    await db_session.commit()

    # Second call should be a no-op (WHERE pr_id IS NULL guard)
    await attach_pr_to_ticket(ticket_id, pr_id=second_pr_id, session=db_session)
    await db_session.commit()

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.pr_id == first_pr_id


@pytest.mark.service
async def test_set_workflow_execution(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    source_external_id = f"myorg/repo#{uuid4().hex[:6]}"
    idempotency_key = f"delivery-{uuid4().hex}"
    workflow_execution_id = uuid4()

    ticket_id, _ = await upsert_ticket_for_pr(
        org_id=org_id,
        source_external_id=source_external_id,
        title="PR",
        description=None,
        repo_external_id="myorg/repo",
        plugin_id="github",
        idempotency_key=idempotency_key,
        payload={},
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None

    await set_workflow_execution(ticket_id, workflow_execution_id=workflow_execution_id, session=db_session)
    await db_session.commit()

    # Verify by reading the row directly
    from sqlalchemy import select  # noqa: PLC0415

    from app.domain.tickets.models import TicketRow  # noqa: PLC0415

    async with db_session.begin_nested():
        row = (await db_session.execute(select(TicketRow).where(TicketRow.id == ticket_id))).scalar_one()
    assert row.current_workflow_execution_id == workflow_execution_id
