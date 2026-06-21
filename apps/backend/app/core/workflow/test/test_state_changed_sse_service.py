"""Service test: every `wfx.state =` site emits `workflow_state_changed`.

Drives a tiny local-only workflow through the engine and asserts the SSE
subscriber receives one `workflow_state_changed` per transition, with the
new state value carried on the payload. Catches the most common
regression — adding a new state assignment without wiring the publish.

The org_id used here matches the value passed in via `workflow_input` so
`_workflow_org_id` resolves it through the fallback path (no
`OrgContextMiddleware` in this test harness).
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.core.sse import subscribe_general
from app.core.tasks import drain_once, get_broker, get_pending_task_names
from app.core.workflow import (
    CommandContext,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowEngine,
    WorkflowState,
    step,
)
from app.core.workflow.models import WorkflowExecutionRow

pytestmark = pytest.mark.service


class _SSETestInput(BaseModel):
    """Minimal workflow_input carrying org_id for SSE routing fallback."""

    org_id: str


class _LocalA:
    kind = "SSEStateA"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


class _LocalB:
    kind = "SSEStateB"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


async def _drain_workflow_outbox(db_session, *, max_iterations: int = 50) -> int:
    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    total = 0
    for _ in range(max_iterations):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return total
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        total += delivered
        if delivered == 0:
            break
    return total


@pytest.fixture(autouse=True)
def _reset_engine():
    import app.core.workflow.service as svc  # noqa: PLC0415

    prior = svc._engine
    svc._engine = None
    yield
    svc._engine = prior


@pytest.mark.asyncio
async def test_local_workflow_emits_workflow_state_changed_per_transition(db_session, redis_or_skip) -> None:
    """Run a 2-step local workflow; assert each state transition emitted
    one `workflow_state_changed` event carrying the new state."""
    org_id = uuid4()
    eng = WorkflowEngine()
    a_step = step(_LocalA)
    b_step = step(_LocalB)
    eng.register_workflow(
        Workflow(
            name="state_changed_v1",
            version=1,
            steps=(a_step, b_step),
            entry=a_step,
            transitions={
                a_step: {"success": b_step},
                b_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
            },
        )
    )
    import app.core.workflow.service as svc  # noqa: PLC0415

    svc._engine = eng

    received: list[dict] = []

    async def _consume() -> None:
        async for event in subscribe_general(org_id):
            if event.get("kind") == "workflow_state_changed":
                received.append(event)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)

    exec_id = await eng.start(
        workflow_name="state_changed_v1",
        ticket_id=str(uuid4()),
        workflow_input=_SSETestInput(org_id=str(org_id)),
        session=db_session,
    )
    await db_session.commit()

    await _drain_workflow_outbox(db_session)

    # Let after-commit publishes flush to Redis subscribers.
    await asyncio.sleep(0.3)

    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    wfx = (
        await db_session.execute(select(WorkflowExecutionRow).where(WorkflowExecutionRow.id == exec_id))
    ).scalar_one()
    assert wfx.state == WorkflowState.DONE.value

    # We must see one running event (initial bootstrap), running events for
    # each step transition, and the terminal `done`. There must be a `done`
    # event — the missing case the coverage walk guards against.
    kinds_states = [(e["kind"], e["state"]) for e in received]
    states_seen = {s for (_, s) in kinds_states}
    assert "running" in states_seen, kinds_states
    assert "done" in states_seen, kinds_states
    # All events carry workflow_execution_id + ticket_id metadata.
    for ev in received:
        assert ev["workflow_execution_id"] == exec_id
        assert "ticket_id" in ev


@pytest.mark.asyncio
async def test_failed_workflow_emits_failed_state_event(db_session, redis_or_skip) -> None:
    """A workflow that fails terminally must emit `workflow_state_changed`
    carrying `state=failed`. This is the failure path's coverage assertion."""
    org_id = uuid4()

    class _Failing:
        kind = "SSEFailingF"
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
            del inputs, ctx, session
            return Outcome.failure(reason="planned")

    eng = WorkflowEngine()
    f_step = step(_Failing)
    eng.register_workflow(
        Workflow(
            name="failing_v1",
            version=1,
            steps=(f_step,),
            entry=f_step,
            transitions={f_step: {"failure": TerminalAction.FAIL_WORKFLOW}},
        )
    )
    import app.core.workflow.service as svc  # noqa: PLC0415

    svc._engine = eng

    received: list[dict] = []

    async def _consume() -> None:
        async for event in subscribe_general(org_id):
            if event.get("kind") == "workflow_state_changed":
                received.append(event)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.1)

    exec_id = await eng.start(
        workflow_name="failing_v1",
        ticket_id=str(uuid4()),
        workflow_input=_SSETestInput(org_id=str(org_id)),
        session=db_session,
    )
    await db_session.commit()

    await _drain_workflow_outbox(db_session)
    await asyncio.sleep(0.3)

    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    states = [e["state"] for e in received if e["workflow_execution_id"] == exec_id]
    assert "failed" in states, states
