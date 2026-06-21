"""Service tests for the `core/workflow` query ops.

Covers the public read-projection API in `core/workflow/views.py`:

- `get_execution_summary` — found and not-found paths.
- `get_awaiting_human_execution` — most recent awaiting_human row; None otherwise.
- `list_active_execution_ids` — excludes terminal states.
- `list_hitl_history` — ordered entries; empty when no decisions exist.
- `list_workflow_states` — all state values returned.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.workflow import (
    get_awaiting_human_execution,
    get_execution_summary,
    list_active_execution_ids,
    list_hitl_history,
    list_workflow_states,
)
from app.core.workflow.models import PendingHumanDecisionRow, WorkflowExecutionRow

pytestmark = pytest.mark.service


def _make_wfx(ticket_id, state: str = "running") -> WorkflowExecutionRow:
    return WorkflowExecutionRow(
        ticket_id=ticket_id,
        workflow_name="pr_review_v1",
        workflow_version=1,
        state=state,
        current_step_id=None,
        pending_agent_command_id=None,
        step_state={},
        cancel_requested=False,
        otel_trace_context=None,
    )


@pytest.mark.asyncio
async def test_get_execution_summary_found(db_session) -> None:
    ticket_id = uuid4()
    row = _make_wfx(ticket_id, "running")
    db_session.add(row)
    await db_session.flush()

    summary = await get_execution_summary(row.id, session=db_session)
    assert summary is not None
    assert summary.id == row.id
    assert summary.ticket_id == ticket_id
    assert summary.state == "running"
    assert summary.workflow_name == "pr_review_v1"


@pytest.mark.asyncio
async def test_get_execution_summary_not_found(db_session) -> None:
    result = await get_execution_summary(uuid4(), session=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_get_awaiting_human_execution_returns_most_recent(db_session) -> None:
    ticket_id = uuid4()
    r_done = _make_wfx(ticket_id, "done")
    r_awaiting = _make_wfx(ticket_id, "awaiting_human")
    db_session.add(r_done)
    db_session.add(r_awaiting)
    await db_session.flush()

    result = await get_awaiting_human_execution(ticket_id, session=db_session)
    assert result is not None
    assert result.state == "awaiting_human"


@pytest.mark.asyncio
async def test_get_awaiting_human_execution_none_when_absent(db_session) -> None:
    ticket_id = uuid4()
    db_session.add(_make_wfx(ticket_id, "done"))
    await db_session.flush()

    result = await get_awaiting_human_execution(ticket_id, session=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_list_active_execution_ids_excludes_terminal(db_session) -> None:
    ticket_id = uuid4()
    active = _make_wfx(ticket_id, "running")
    done = _make_wfx(ticket_id, "done")
    failed = _make_wfx(ticket_id, "failed")
    cancelled = _make_wfx(ticket_id, "cancelled")
    db_session.add_all([active, done, failed, cancelled])
    await db_session.flush()

    ids = await list_active_execution_ids(ticket_id, session=db_session)
    assert active.id in ids
    assert done.id not in ids
    assert failed.id not in ids
    assert cancelled.id not in ids


@pytest.mark.asyncio
async def test_list_active_execution_ids_empty_when_all_terminal(db_session) -> None:
    ticket_id = uuid4()
    db_session.add(_make_wfx(ticket_id, "done"))
    await db_session.flush()

    ids = await list_active_execution_ids(ticket_id, session=db_session)
    assert ids == []


@pytest.mark.asyncio
async def test_list_hitl_history_returns_decisions_for_ticket(db_session) -> None:
    ticket_id = uuid4()
    row = _make_wfx(ticket_id, "awaiting_human")
    db_session.add(row)
    await db_session.flush()

    d1 = PendingHumanDecisionRow(
        workflow_execution_id=row.id,
        question_payload={"q": "first"},
        resolution_payload={"ans": "yes"},
    )
    d2 = PendingHumanDecisionRow(
        workflow_execution_id=row.id,
        question_payload={"q": "second"},
        resolution_payload=None,
    )
    db_session.add(d1)
    db_session.add(d2)
    await db_session.flush()

    entries = await list_hitl_history(ticket_id, session=db_session)
    assert len(entries) == 2
    # All entries belong to the correct workflow execution.
    assert all(e.workflow_execution_id == row.id for e in entries)
    # Question payloads are projected correctly.
    payloads = {e.question_payload["q"] for e in entries}
    assert payloads == {"first", "second"}
    # Resolution payload projected.
    resolved = next(e for e in entries if e.resolution_payload is not None)
    assert resolved.resolution_payload == {"ans": "yes"}


@pytest.mark.asyncio
async def test_list_hitl_history_empty_when_no_executions(db_session) -> None:
    entries = await list_hitl_history(uuid4(), session=db_session)
    assert entries == []


@pytest.mark.asyncio
async def test_list_workflow_states_returns_all(db_session) -> None:
    ticket_id = uuid4()
    db_session.add(_make_wfx(ticket_id, "running"))
    db_session.add(_make_wfx(ticket_id, "done"))
    await db_session.flush()

    states = await list_workflow_states(session=db_session)
    assert "running" in states
    assert "done" in states
