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
    CommandCategory,
    CommandContext,
    Outcome,
    Step,
    Workflow,
    WorkflowRunView,
    list_run_views_for_ticket,
)
from app.core.workflow.models import WorkflowExecutionRow
from app.testing.workflow_harness import scoped_engine, scoped_workflow

pytestmark = pytest.mark.service


class _NoopLocal:
    category = CommandCategory.LOCAL
    restart_safe = True

    def __init__(self, kind: str) -> None:
        self.kind = kind

    async def execute(self, inputs, ctx: CommandContext) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


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
    with scoped_engine():
        wf = Workflow(
            name="test_runview_v1",
            version=1,
            steps=(
                Step(id="alpha", command_kind="NoopAlpha"),
                Step(id="beta", command_kind="NoopBeta"),
                Step(id="gamma", command_kind="NoopGamma"),
            ),
            entry_step_id="alpha",
        )
        # Register the commands so engine.get_workflow returns the def
        # without complaining about command kinds.
        from app.core.workflow import get_engine  # noqa: PLC0415

        eng = get_engine()
        eng.register_command(_NoopLocal("NoopAlpha"))
        eng.register_command(_NoopLocal("NoopBeta"))
        eng.register_command(_NoopLocal("NoopGamma"))
        with scoped_workflow(wf):
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
        current_step_id="beta",
        step_state={
            "alpha": {
                "outcome_label": "success",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
            "beta": {"started_at": t1},
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
    assert by_id["alpha"].state == "done"
    assert by_id["alpha"].started_at is not None
    assert by_id["alpha"].completed_at is not None
    assert by_id["beta"].state == "running"
    assert by_id["beta"].started_at is not None
    assert by_id["beta"].completed_at is None
    assert by_id["gamma"].state == "pending"
    assert by_id["gamma"].started_at is None
    assert by_id["gamma"].completed_at is None


@pytest.mark.asyncio
async def test_runview_projects_failed_step(db_session, _engine_with_three_step_workflow) -> None:
    """A non-success non-`_skipped` outcome label projects as `failed`."""
    ticket_id = uuid4()
    t1 = datetime.now(UTC).isoformat()
    row = _wfx(
        ticket_id,
        state="failed",
        current_step_id="alpha",
        step_state={
            "alpha": {
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
    assert by_id["alpha"].state == "failed"
    # Other steps untouched → pending (terminal execution state, never ran).
    assert by_id["beta"].state == "pending"
    assert by_id["gamma"].state == "pending"


@pytest.mark.asyncio
async def test_runview_projects_skipped_step(db_session, _engine_with_three_step_workflow) -> None:
    """Explicit `_skipped` outcome label projects as `skipped`."""
    ticket_id = uuid4()
    t1 = datetime.now(UTC).isoformat()
    row = _wfx(
        ticket_id,
        state="done",
        step_state={
            "alpha": {
                "outcome_label": "_skipped",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
            "beta": {
                "outcome_label": "success",
                "outputs": {},
                "started_at": t1,
                "completed_at": t1,
            },
            "gamma": {
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
    assert by_id["alpha"].state == "skipped"
    assert by_id["beta"].state == "done"
    assert by_id["gamma"].state == "done"


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
        current_step_id="alpha",
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
    with scoped_engine():
        ticket_id = uuid4()
        row = _wfx(ticket_id, state="running", workflow_name="never_registered_v1")
        db_session.add(row)
        await db_session.flush()

        runs = await list_run_views_for_ticket(ticket_id, session=db_session)
        assert len(runs) == 1
        assert runs[0].steps == ()
        assert runs[0].workflow_name == "never_registered_v1"
