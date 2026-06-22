"""Service tests for `list_run_views_for_ticket` — the Ticket page projection.

Covers:
- Step-state derivation across the five projected branches
  (pending / running / done / failed / skipped).
- `started_at` / `completed_at` carried through from `step_state`.
- Newest-first ordering for multi-run tickets.
- Empty-step-list when the workflow definition is unknown to the engine.

State derivation is the load-bearing piece — the SPA's stage band reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.workflow import (
    CommandContext,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowRunView,
    list_run_views_for_ticket,
    step,
)
from app.core.workflow.models import WorkflowExecutionRow
from app.testing.workflow_harness import set_engine_for_tests

pytestmark = pytest.mark.service


class _NoopAlpha:
    kind = "NoopAlpha"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


class _NoopBeta:
    kind = "NoopBeta"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


class _NoopGamma:
    kind = "NoopGamma"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


# Step ids are now command kind names.
_ALPHA_ID = "NoopAlpha"
_BETA_ID = "NoopBeta"
_GAMMA_ID = "NoopGamma"

_alpha_step = step(_NoopAlpha)
_beta_step = step(_NoopBeta)
_gamma_step = step(_NoopGamma)


def _wfx(
    ticket_id,
    *,
    state: str = "running",
    current_step_id: str | None = None,
    step_state: dict | None = None,
    workflow_name: str = "test_runview_v1",
) -> WorkflowExecutionRow:
    return WorkflowExecutionRow(
        ticket_id=ticket_id,
        workflow_name=workflow_name,
        workflow_version=1,
        state=state,
        current_step_id=current_step_id,
        pending_agent_command_id=None,
        step_state=step_state or {},
        cancel_requested=False,
        otel_trace_context=None,
    )


@pytest.fixture
def _engine_with_three_step_workflow():
    """Bind a fresh engine registering a three-step workflow we project against."""
    with set_engine_for_tests() as eng:
        wf = Workflow(
            name="test_runview_v1",
            version=1,
            steps=(_alpha_step, _beta_step, _gamma_step),
            entry=_alpha_step,
            transitions={
                _alpha_step: {"success": _beta_step},
                _beta_step: {"success": _gamma_step},
                _gamma_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
            },
        )
        eng.register_workflow(wf)
        yield


@pytest.mark.asyncio
async def test_runview_projects_done_running_pending_states(
    db_session, _engine_with_three_step_workflow
) -> None:
    """Step-state derivation: done (success outcome), running (current_step_id),
    pending (untouched, no terminal state)."""
    ticket_id = uuid4()
    t1 = datetime.now(UTC).isoformat()
    row = _wfx(
        ticket_id,
        state="running",
        current_step_id=_BETA_ID,
        step_state={
            _ALPHA_ID: {
                "outcome_label": "success",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
            _BETA_ID: {"started_at": t1},
        },
    )
    db_session.add(row)
    await db_session.flush()

    runs = await list_run_views_for_ticket(ticket_id, session=db_session)
    assert len(runs) == 1
    run = runs[0]
    assert isinstance(run, WorkflowRunView)
    assert run.workflow_name == "test_runview_v1"

    by_id = {s.step_id: s for s in run.steps}
    assert by_id[_ALPHA_ID].state == "done"
    assert by_id[_ALPHA_ID].started_at is not None
    assert by_id[_ALPHA_ID].completed_at is not None
    assert by_id[_BETA_ID].state == "running"
    assert by_id[_BETA_ID].started_at is not None
    assert by_id[_BETA_ID].completed_at is None
    assert by_id[_GAMMA_ID].state == "pending"
    assert by_id[_GAMMA_ID].started_at is None
    assert by_id[_GAMMA_ID].completed_at is None


@pytest.mark.asyncio
async def test_runview_projects_failed_step(db_session, _engine_with_three_step_workflow) -> None:
    """A non-success non-`_skipped` outcome label projects as `failed`."""
    ticket_id = uuid4()
    t1 = datetime.now(UTC).isoformat()
    row = _wfx(
        ticket_id,
        state="failed",
        current_step_id=_ALPHA_ID,
        step_state={
            _ALPHA_ID: {
                "outcome_label": "timeout",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
        },
    )
    db_session.add(row)
    await db_session.flush()

    runs = await list_run_views_for_ticket(ticket_id, session=db_session)
    by_id = {s.step_id: s for s in runs[0].steps}
    assert by_id[_ALPHA_ID].state == "failed"
    # Other steps untouched → pending (terminal execution state, never ran).
    assert by_id[_BETA_ID].state == "pending"
    assert by_id[_GAMMA_ID].state == "pending"


@pytest.mark.asyncio
async def test_runview_projects_skipped_step(db_session, _engine_with_three_step_workflow) -> None:
    """Explicit `_skipped` outcome label projects as `skipped`."""
    ticket_id = uuid4()
    t1 = datetime.now(UTC).isoformat()
    row = _wfx(
        ticket_id,
        state="done",
        step_state={
            _ALPHA_ID: {
                "outcome_label": "_skipped",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
            _BETA_ID: {
                "outcome_label": "success",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
            _GAMMA_ID: {
                "outcome_label": "success",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
        },
    )
    db_session.add(row)
    await db_session.flush()

    runs = await list_run_views_for_ticket(ticket_id, session=db_session)
    by_id = {s.step_id: s for s in runs[0].steps}
    assert by_id[_ALPHA_ID].state == "skipped"
    assert by_id[_BETA_ID].state == "done"
    assert by_id[_GAMMA_ID].state == "done"


@pytest.mark.asyncio
async def test_runview_pending_when_terminal_execution_state_and_no_outcome(
    db_session, _engine_with_three_step_workflow
) -> None:
    """A failed/cancelled execution leaves untouched steps as `pending`,
    not `running`, because the execution is not active."""
    ticket_id = uuid4()
    row = _wfx(
        ticket_id,
        state="cancelled",
        current_step_id=_ALPHA_ID,
        step_state={},
    )
    db_session.add(row)
    await db_session.flush()

    runs = await list_run_views_for_ticket(ticket_id, session=db_session)
    states = {s.state for s in runs[0].steps}
    assert states == {"pending"}


@pytest.mark.asyncio
async def test_runview_orders_newest_first(db_session, _engine_with_three_step_workflow) -> None:
    """Multi-run tickets are projected newest-first (latest run at the top)."""
    ticket_id = uuid4()
    older = _wfx(ticket_id, state="done")
    newer = _wfx(ticket_id, state="running")
    # Pin distinct created_at — both rows otherwise share the transaction
    # timestamp, making the ordering assertion vacuous.
    older.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    newer.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    db_session.add(older)
    db_session.add(newer)
    await db_session.flush()

    runs = await list_run_views_for_ticket(ticket_id, session=db_session)
    assert len(runs) == 2
    assert runs[0].created_at > runs[1].created_at
    assert runs[0].state == "running"  # newer run on top
    assert runs[1].state == "done"  # older run below


@pytest.mark.asyncio
async def test_runview_empty_steps_when_workflow_unknown(db_session) -> None:
    """A run row whose workflow def isn't registered yields zero steps but
    still surfaces the execution metadata."""
    with set_engine_for_tests():
        ticket_id = uuid4()
        row = _wfx(ticket_id, state="running", workflow_name="never_registered_v1")
        db_session.add(row)
        await db_session.flush()

        runs = await list_run_views_for_ticket(ticket_id, session=db_session)
        assert len(runs) == 1
        assert runs[0].steps == ()
        assert runs[0].workflow_name == "never_registered_v1"
