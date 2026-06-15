"""Service tests for transition_on_workflow_start.

Covers all guard branches:
- Pending ticket + matching wfx_id → flips to running, returns True.
- Pending ticket + wrong wfx_id → returns False, status unchanged.
- Running ticket (already past pending) → returns False, no event fired.
- Missing ticket → returns False.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.audit_log import list_for_entity
from app.domain.tickets import (
    create_from_pr,
    get,
    set_workflow_execution,
    transition_on_workflow_start,
)

pytestmark = pytest.mark.service


async def _make_pending_ticket(db_session, org_id):  # type: ignore[no-untyped-def]
    """Insert a pending ticket and stamp a workflow execution on it.

    Returns (ticket_id, workflow_execution_id).
    """
    workflow_execution_id = uuid4()
    ticket_id, created = await create_from_pr(
        org_id=org_id,
        source_external_id=f"repo/r#{uuid4().hex[:6]}",
        title="Test PR",
        description=None,
        repo_external_id="repo/r",
        plugin_id="github",
        idempotency_key=f"delivery-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    assert created is True
    await set_workflow_execution(
        ticket_id,
        workflow_execution_id=workflow_execution_id,
        session=db_session,
    )
    await db_session.commit()
    assert ticket_id is not None
    return ticket_id, workflow_execution_id


@pytest.mark.asyncio
async def test_pending_ticket_matching_wfx_flips_to_running(db_session) -> None:  # type: ignore[no-untyped-def]
    """Pending ticket owned by the execution → flips to running, audit row
    written, returns True."""
    org_id = uuid4()
    ticket_id, execution_id = await _make_pending_ticket(db_session, org_id)

    result = await transition_on_workflow_start(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=execution_id,
        session=db_session,
    )
    assert result is True

    await db_session.commit()

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.status == "running"

    entries = await list_for_entity("ticket", ticket_id, org_id=org_id, kinds=["ticket.status_changed"])
    assert len(entries) >= 1
    assert entries[0].kind == "ticket.status_changed"


@pytest.mark.asyncio
async def test_pending_ticket_wrong_wfx_is_no_op(db_session) -> None:  # type: ignore[no-untyped-def]
    """Pending ticket owned by a different execution → no flip, returns False."""
    org_id = uuid4()
    ticket_id, _execution_id = await _make_pending_ticket(db_session, org_id)

    different_execution_id = uuid4()
    result = await transition_on_workflow_start(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=different_execution_id,
        session=db_session,
    )
    assert result is False

    await db_session.commit()
    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.status == "pending"


@pytest.mark.asyncio
async def test_already_running_ticket_is_no_op(db_session) -> None:  # type: ignore[no-untyped-def]
    """Ticket already in running state → returns False, no transition applied."""
    from sqlalchemy import update  # noqa: PLC0415

    from app.domain.tickets.models import TicketRow  # noqa: PLC0415

    org_id = uuid4()
    ticket_id, execution_id = await _make_pending_ticket(db_session, org_id)

    # Force the ticket to running directly, bypassing the hook.
    await db_session.execute(update(TicketRow).where(TicketRow.id == ticket_id).values(status="running"))
    await db_session.commit()

    result = await transition_on_workflow_start(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=execution_id,
        session=db_session,
    )
    assert result is False

    await db_session.commit()
    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.status == "running"  # unchanged


@pytest.mark.asyncio
async def test_missing_ticket_returns_false(db_session) -> None:  # type: ignore[no-untyped-def]
    """Non-existent ticket → returns False, never raises."""
    org_id = uuid4()
    missing_id = uuid4()

    result = await transition_on_workflow_start(
        missing_id,
        org_id=org_id,
        workflow_execution_id=uuid4(),
        session=db_session,
    )
    assert result is False


@pytest.mark.asyncio
async def test_no_commit_inside_fn(db_session) -> None:  # type: ignore[no-untyped-def]
    """The fn must not commit — the session must still be in a transaction
    after the call returns."""
    org_id = uuid4()
    ticket_id, execution_id = await _make_pending_ticket(db_session, org_id)

    result = await transition_on_workflow_start(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=execution_id,
        session=db_session,
    )
    assert result is True
    assert db_session.in_transaction()
