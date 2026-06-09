"""Service tests for transition_on_workflow_terminal.

Covers all guard branches:
- Owner + running ticket → flips to done/failed/cancelled, audit row present,
  returns True.
- Non-owner (current_workflow_execution_id differs) → no-op, returns False.
- Already-terminal ticket → no-op (idempotent), returns False.
- Missing ticket or wrong org → returns False, never raises.
- No commit inside the fn — state is only visible after the caller commits.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import update

from app.core.audit_log import list_for_entity
from app.domain.tickets import (
    get,
    set_workflow_execution,
    transition_on_workflow_terminal,
    upsert_ticket_for_pr,
)
from app.domain.tickets.models import TicketRow

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_running_ticket(db_session, org_id) -> tuple:  # type: ignore[no-untyped-def]
    """Insert a running ticket and stamp a workflow execution on it.

    Returns (ticket_id, workflow_execution_id).
    """
    workflow_execution_id = uuid4()
    ticket_id, created = await upsert_ticket_for_pr(
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.parametrize("to_status", ["done", "failed", "cancelled"])
async def test_owner_running_ticket_flips_status(db_session, to_status) -> None:  # type: ignore[no-untyped-def]
    """When the execution still owns the ticket and it is running, the
    transition applies: status changes, audit row written, returns True."""
    org_id = uuid4()
    ticket_id, execution_id = await _make_running_ticket(db_session, org_id)

    result = await transition_on_workflow_terminal(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=execution_id,
        to_status=to_status,
        reason="workflow finished",
        session=db_session,
    )
    assert result is True

    # Commit so subsequent reads (including list_for_entity's own session) see the change.
    await db_session.commit()

    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.status == to_status

    # Audit row must be present.
    entries = await list_for_entity("ticket", ticket_id, org_id=org_id, kinds=["ticket.status_changed"])
    assert len(entries) >= 1
    assert entries[0].kind == "ticket.status_changed"


@pytest.mark.service
@pytest.mark.asyncio
async def test_no_commit_inside_fn(db_session) -> None:  # type: ignore[no-untyped-def]
    """The fn must not commit — the session must still be in a transaction
    after the call returns."""
    org_id = uuid4()
    ticket_id, execution_id = await _make_running_ticket(db_session, org_id)

    result = await transition_on_workflow_terminal(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=execution_id,
        to_status="done",
        reason=None,
        session=db_session,
    )
    assert result is True

    # The fn must not have called commit() — the session must still be in
    # a transaction (the SAVEPOINT is still active if commit was not called).
    assert db_session.in_transaction()


@pytest.mark.service
@pytest.mark.asyncio
async def test_non_owner_is_no_op(db_session) -> None:  # type: ignore[no-untyped-def]
    """When current_workflow_execution_id differs, the call is a no-op."""
    org_id = uuid4()
    ticket_id, _execution_id = await _make_running_ticket(db_session, org_id)

    different_execution_id = uuid4()
    result = await transition_on_workflow_terminal(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=different_execution_id,
        to_status="done",
        reason=None,
        session=db_session,
    )
    assert result is False

    await db_session.commit()
    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.status == "running"


@pytest.mark.service
@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["done", "failed", "cancelled"])
async def test_already_terminal_is_idempotent_no_op(db_session, terminal) -> None:  # type: ignore[no-untyped-def]
    """Calling on an already-terminal ticket returns False without modifying state."""
    org_id = uuid4()
    ticket_id, execution_id = await _make_running_ticket(db_session, org_id)

    # Force the ticket into the terminal state directly so we can test the guard
    # without going through the public API that would raise on double-transition.
    await db_session.execute(update(TicketRow).where(TicketRow.id == ticket_id).values(status=terminal))
    await db_session.commit()

    result = await transition_on_workflow_terminal(
        ticket_id,
        org_id=org_id,
        workflow_execution_id=execution_id,
        to_status="done",
        reason=None,
        session=db_session,
    )
    assert result is False

    await db_session.commit()
    ticket = await get(ticket_id, org_id=org_id)
    assert ticket.status == terminal  # unchanged


@pytest.mark.service
@pytest.mark.asyncio
async def test_missing_ticket_returns_false(db_session) -> None:  # type: ignore[no-untyped-def]
    """A ticket_id that does not exist returns False and never raises."""
    org_id = uuid4()
    result = await transition_on_workflow_terminal(
        uuid4(),
        org_id=org_id,
        workflow_execution_id=uuid4(),
        to_status="done",
        reason=None,
        session=db_session,
    )
    assert result is False


@pytest.mark.service
@pytest.mark.asyncio
async def test_wrong_org_returns_false(db_session) -> None:  # type: ignore[no-untyped-def]
    """A ticket that exists but belongs to a different org returns False."""
    real_org_id = uuid4()
    ticket_id, execution_id = await _make_running_ticket(db_session, real_org_id)

    wrong_org_id = uuid4()
    result = await transition_on_workflow_terminal(
        ticket_id,
        org_id=wrong_org_id,
        workflow_execution_id=execution_id,
        to_status="done",
        reason=None,
        session=db_session,
    )
    assert result is False

    await db_session.commit()
    ticket = await get(ticket_id, org_id=real_org_id)
    assert ticket.status == "running"  # untouched
