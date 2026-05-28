"""End-to-end state-machine coverage for the workflow engine.

Drives the three task bodies (`start_step`, `handle_agent_event`,
`route_workflow`) directly. A test-side `drain` coroutine pulls outbox
rows and re-dispatches them to the task bodies, simulating the worker
without standing up Redis or taskiq.

Covers:
- Local-only workflow runs to completion.
- Workspace step async cycle: start_step → awaiting_agent → terminal event → route → done.
- Failure + retry → fail_workflow after exhaustion.
- HITL pause + resume.
- `append_steps` inserts at front of remaining sequence.
- Cancellation during `awaiting_agent`: cancel + event → cancelled.
- Stale event handling: duplicate event is a no-op.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.core.tasks import OutboxEntryRow, drain_once
from app.core.workflow import (
    HANDLE_AGENT_EVENT,
    ROUTE_WORKFLOW,
    CommandCategory,
    CommandContext,
    Outcome,
    PendingHumanDecisionRow,
    Step,
    TerminalAction,
    Workflow,
    WorkflowEngine,
    WorkflowExecutionRow,
    WorkflowState,
    request_cancel,
    resume_hitl,
)
from app.core.workspace import (
    clear_recovery_policies,
    register_recovery_policy,
)

# ── Test commands ───────────────────────────────────────────────────────


class _RecordingLocal:
    """Local command that records each invocation and returns Outcome.success
    with the supplied outputs."""

    def __init__(self, kind: str, outputs: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.category = CommandCategory.LOCAL
        self.restart_safe = True
        self._outputs = outputs or {}
        self.calls: list[dict] = []

    async def execute(self, inputs: BaseModel | dict, ctx: CommandContext) -> Outcome:
        self.calls.append({"inputs": dict(inputs), "step_id": ctx.step_id, "attempt": ctx.attempt})
        return Outcome.success(outputs=self._outputs)


class _FailingLocal:
    """Local command that returns Outcome.failure(); used for retry tests."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.category = CommandCategory.LOCAL
        self.restart_safe = True
        self.calls: int = 0

    async def execute(self, inputs: BaseModel | dict, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        self.calls += 1
        return Outcome.failure(reason="planned-failure")


class _RaisingLocal:
    """Local command that raises — _safe_execute should catch and turn it
    into a failure outcome."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.category = CommandCategory.LOCAL
        self.restart_safe = True

    async def execute(self, inputs: BaseModel | dict, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        raise RuntimeError("unexpected")


class _HitlAsk:
    kind = "AskHuman"
    category = CommandCategory.HITL
    restart_safe = True

    async def execute(self, inputs: BaseModel | dict, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.hitl_pending(question={"prompt": "approve?"})


class _AppendOnce:
    """First call returns Outcome.success with append_steps; subsequent calls
    just return success. Used to verify append_steps insertion."""

    def __init__(self, kind: str, extra: tuple[Step, ...]) -> None:
        self.kind = kind
        self.category = CommandCategory.LOCAL
        self.restart_safe = True
        self._extra = extra
        self._fired = False

    async def execute(self, inputs: BaseModel | dict, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        if not self._fired:
            self._fired = True
            return Outcome.success(append_steps=self._extra)
        return Outcome.success()


class _WorkspaceStub:
    """Workspace-category command whose dispatch start_step stubs.
    Provided here so the workspace branch of start_step is exercised."""

    kind = "DoOnAgent"
    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs: BaseModel | dict, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


# ── Drain helper ────────────────────────────────────────────────────────


async def _drain_workflow_outbox(db_session, *, max_iterations: int = 50) -> int:
    """Pull `taskiq_enqueue` rows out of the outbox and re-dispatch them
    into the matching task body via the broker's task registry. Loops until
    the outbox is empty or `max_iterations` hit (a runaway loop is a bug)."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    total = 0
    for _ in range(max_iterations):
        rows = (
            (
                await db_session.execute(
                    select(OutboxEntryRow)
                    .where(OutboxEntryRow.dispatched_at.is_(None))
                    .order_by(OutboxEntryRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return total

        async def _dispatcher(kind: str, payload: dict) -> None:
            assert kind == "taskiq_enqueue"
            decorated = get_broker().find_task(payload["task_name"])
            if decorated is None:
                raise RuntimeError(f"no registered task body for {payload['task_name']}")
            await decorated.original_func(**payload["args"])

        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        total += delivered
        if delivered == 0:
            break
    return total


# ── Engine fixture ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_engine():  # type: ignore[no-untyped-def]
    import app.core.workflow.service as svc  # noqa: PLC0415

    prior = svc._engine
    svc._engine = None
    yield
    svc._engine = prior


def _engine_with(*commands: Any, workflow: Workflow) -> WorkflowEngine:
    eng = WorkflowEngine()
    for c in commands:
        eng.register_command(c)
    eng.register_workflow(workflow)
    # Install as the process singleton so task bodies pick it up.
    import app.core.workflow.service as svc  # noqa: PLC0415

    svc._engine = eng
    return eng


def _ticket_id() -> str:
    return str(uuid4())


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_only_workflow_runs_to_done(db_session) -> None:
    a = _RecordingLocal("A", outputs={"out_a": 1})
    b = _RecordingLocal("B", outputs={"out_b": 2})
    wf = Workflow(
        name="local-2",
        version=1,
        steps=(
            Step(id="s1", command_kind="A"),
            Step(id="s2", command_kind="B", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}),
        ),
        entry_step_id="s1",
    )
    eng = _engine_with(a, b, workflow=wf)
    exec_id = await eng.start(workflow_name="local-2", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()

    await _drain_workflow_outbox(db_session)

    wfx = (
        await db_session.execute(select(WorkflowExecutionRow).where(WorkflowExecutionRow.id == exec_id))
    ).scalar_one()
    assert wfx.state == WorkflowState.DONE.value
    assert wfx.current_step_id == "s2"
    assert wfx.step_state["s1"]["outputs"] == {"out_a": 1}
    assert wfx.step_state["s2"]["outputs"] == {"out_b": 2}
    assert len(a.calls) == 1
    assert len(b.calls) == 1


@pytest.mark.asyncio
async def test_failure_no_retry_routes_to_fail_workflow(db_session) -> None:
    fail = _FailingLocal("Bad")
    wf = Workflow(
        name="bad-1",
        version=1,
        steps=(Step(id="only", command_kind="Bad"),),
        entry_step_id="only",
    )
    eng = _engine_with(fail, workflow=wf)
    exec_id = await eng.start(workflow_name="bad-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.FAILED.value
    assert fail.calls == 1  # default retry_policy.max_attempts = 1 → no retry


@pytest.mark.asyncio
async def test_failure_with_retry_eventually_fails(db_session) -> None:
    from app.core.workflow import RetryPolicy  # noqa: PLC0415

    fail = _FailingLocal("Bad")
    wf = Workflow(
        name="retry-3",
        version=1,
        steps=(
            Step(
                id="only",
                command_kind="Bad",
                retry_policy=RetryPolicy(max_attempts=3),
            ),
        ),
        entry_step_id="only",
    )
    eng = _engine_with(fail, workflow=wf)
    exec_id = await eng.start(workflow_name="retry-3", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.FAILED.value
    assert fail.calls == 3


@pytest.mark.asyncio
async def test_raising_command_becomes_failure(db_session) -> None:
    raiser = _RaisingLocal("Boom")
    wf = Workflow(
        name="raise-1",
        version=1,
        steps=(Step(id="only", command_kind="Boom"),),
        entry_step_id="only",
    )
    eng = _engine_with(raiser, workflow=wf)
    exec_id = await eng.start(workflow_name="raise-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.FAILED.value


@pytest.mark.asyncio
async def test_input_resolution_pulls_prior_step_outputs(db_session) -> None:
    a = _RecordingLocal("A", outputs={"workspace_id": "ws-123"})
    b = _RecordingLocal("B")
    wf = Workflow(
        name="resolve-1",
        version=1,
        steps=(
            Step(id="provision", command_kind="A"),
            Step(
                id="use",
                command_kind="B",
                inputs={"workspace_id": "$provision.workspace_id"},
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="provision",
    )
    eng = _engine_with(a, b, workflow=wf)
    exec_id = await eng.start(workflow_name="resolve-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value
    assert b.calls[0]["inputs"] == {"workspace_id": "ws-123"}


@pytest.mark.asyncio
async def test_append_steps_inserts_at_front(db_session) -> None:
    inserted = _RecordingLocal("Inserted", outputs={"i": True})
    appender = _AppendOnce("Appender", extra=(Step(id="inserted", command_kind="Inserted"),))
    tail = _RecordingLocal("Tail")
    wf = Workflow(
        name="append-1",
        version=1,
        steps=(
            Step(id="appender", command_kind="Appender"),
            Step(id="tail", command_kind="Tail", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}),
        ),
        entry_step_id="appender",
    )
    eng = _engine_with(appender, inserted, tail, workflow=wf)
    exec_id = await eng.start(workflow_name="append-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value
    # The inserted step ran between appender and tail.
    assert len(inserted.calls) == 1
    assert len(tail.calls) == 1


@pytest.mark.asyncio
async def test_workspace_step_transitions_to_awaiting_agent(db_session) -> None:
    ws = _WorkspaceStub()
    wf = Workflow(
        name="ws-1",
        version=1,
        steps=(Step(id="do", command_kind="DoOnAgent"),),
        entry_step_id="do",
    )
    eng = _engine_with(ws, workflow=wf)
    # remote_agent provider: Workspace branch dispatches over the wire and
    # parks the workflow in awaiting_agent until the terminal AgentEvent.
    exec_id = await eng.start(
        workflow_name="ws-1",
        ticket_id=_ticket_id(),
        workspace_provider="remote_agent",
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.AWAITING_AGENT.value
    assert wfx.pending_agent_command_id is not None
    assert wfx.current_step_id == "do"


@pytest.mark.asyncio
async def test_in_memory_workspace_step_runs_inline_to_done(db_session) -> None:
    """In-memory provider: the Workspace branch collapses into an inline
    `execute()` call (no wire round-trip). Workflow advances straight to
    the next step or terminal state without ever entering awaiting_agent."""
    ws = _WorkspaceStub()
    wf = Workflow(
        name="ws-inline-1",
        version=1,
        steps=(
            Step(
                id="do",
                command_kind="DoOnAgent",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="do",
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-inline-1",
        ticket_id=_ticket_id(),
        workspace_provider="in_memory",
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value
    assert wfx.pending_agent_command_id is None


@pytest.mark.asyncio
async def test_handle_agent_event_advances_workflow(db_session) -> None:
    ws = _WorkspaceStub()
    wf = Workflow(
        name="ws-then-done",
        version=1,
        steps=(
            Step(
                id="do", command_kind="DoOnAgent", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}
            ),
        ),
        entry_step_id="do",
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-then-done",
        ticket_id=_ticket_id(),
        workspace_provider="remote_agent",
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    pending_id = wfx.pending_agent_command_id
    assert pending_id is not None

    # Simulate the agent terminal event by enqueueing handle_agent_event.
    from app.core.tasks import enqueue  # noqa: PLC0415

    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": str(exec_id),
            "agent_command_id": str(pending_id),
            "outcome_label": "success",
            "outputs": {"result": "ok"},
            "traceparent": None,
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value
    assert wfx.pending_agent_command_id is None
    assert wfx.step_state["do"]["outputs"] == {"result": "ok"}


@pytest.mark.asyncio
async def test_stale_handle_agent_event_is_noop(db_session) -> None:
    ws = _WorkspaceStub()
    wf = Workflow(
        name="ws-stale",
        version=1,
        steps=(
            Step(
                id="do", command_kind="DoOnAgent", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}
            ),
        ),
        entry_step_id="do",
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-stale",
        ticket_id=_ticket_id(),
        workspace_provider="remote_agent",
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    pending_id = wfx.pending_agent_command_id
    assert pending_id is not None

    # First event advances the workflow to DONE.
    from app.core.tasks import enqueue  # noqa: PLC0415

    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": str(exec_id),
            "agent_command_id": str(pending_id),
            "outcome_label": "success",
            "outputs": {},
            "traceparent": None,
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    # Duplicate event arrives — must be a no-op (state stays DONE).
    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": str(exec_id),
            "agent_command_id": str(pending_id),
            "outcome_label": "success",
            "outputs": {},
            "traceparent": None,
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value


@pytest.mark.asyncio
async def test_hitl_pause_and_resume(db_session) -> None:
    asker = _HitlAsk()
    tail = _RecordingLocal("Tail")
    wf = Workflow(
        name="hitl-1",
        version=1,
        steps=(
            Step(id="ask", command_kind="AskHuman", hitl=True),
            Step(
                id="tail",
                command_kind="Tail",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="ask",
    )
    eng = _engine_with(asker, tail, workflow=wf)
    exec_id = await eng.start(workflow_name="hitl-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.AWAITING_HUMAN.value

    pending = (
        await db_session.execute(
            select(PendingHumanDecisionRow).where(PendingHumanDecisionRow.workflow_execution_id == wfx.id)
        )
    ).scalar_one()
    assert pending.question_payload == {"prompt": "approve?"}
    assert pending.resolved_at is None

    # Resume with a response.
    resumed = await resume_hitl(str(exec_id), response={"decision": "approve"}, session=db_session)
    assert resumed is True
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value
    pending = (
        await db_session.execute(
            select(PendingHumanDecisionRow).where(PendingHumanDecisionRow.workflow_execution_id == wfx.id)
        )
    ).scalar_one()
    assert pending.resolved_at is not None
    assert pending.resolution_payload == {"decision": "approve"}


@pytest.mark.asyncio
async def test_request_cancel_during_awaiting_agent_then_event(db_session) -> None:
    ws = _WorkspaceStub()
    wf = Workflow(
        name="ws-cancel",
        version=1,
        steps=(
            Step(
                id="do", command_kind="DoOnAgent", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}
            ),
        ),
        entry_step_id="do",
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-cancel",
        ticket_id=_ticket_id(),
        workspace_provider="remote_agent",
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.AWAITING_AGENT.value
    pending_id = wfx.pending_agent_command_id

    # User cancels while waiting for the agent.
    ok = await request_cancel(str(exec_id), session=db_session)
    assert ok is True
    await db_session.commit()

    # Event finally arrives — route_workflow should observe cancel_requested
    # and transition to cancelled.
    from app.core.tasks import enqueue  # noqa: PLC0415

    await enqueue(
        HANDLE_AGENT_EVENT,
        args={
            "workflow_execution_id": str(exec_id),
            "agent_command_id": str(pending_id),
            "outcome_label": "success",
            "outputs": {},
            "traceparent": None,
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.CANCELLED.value


@pytest.mark.asyncio
async def test_route_workflow_skips_when_terminal(db_session) -> None:
    """An out-of-order route_workflow against a terminal workflow is a no-op."""
    from app.core.tasks import enqueue  # noqa: PLC0415

    noop = _RecordingLocal("Noop")
    wf = Workflow(
        name="term-skip",
        version=1,
        steps=(Step(id="only", command_kind="Noop"),),
        entry_step_id="only",
    )
    eng = _engine_with(noop, workflow=wf)
    exec_id = await eng.start(workflow_name="term-skip", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value

    # Synthesize a late route_workflow firing via the outbox — should be a no-op.
    await enqueue(
        ROUTE_WORKFLOW,
        args={
            "workflow_execution_id": str(exec_id),
            "completed_step_id": "only",
            "outcome_label": "success",
            "outputs": {},
            "traceparent": None,
        },
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value


@pytest.mark.asyncio
async def test_recovery_policy_inserts_recovery_step_before_retry(db_session) -> None:
    """When a Local command fails with a label that has a registered
    recovery policy, the engine inserts the recovery command as an
    appended step that runs BEFORE the failed step retries. Recovery
    fires at most once per step instance."""
    clear_recovery_policies()
    register_recovery_policy(failure_label="auth_expired", command_kind="DoRefresh")

    review_calls: list[str] = []
    refresh_calls: list[str] = []

    class _FailOnceCommand:
        kind = "DoReview"
        category = CommandCategory.LOCAL
        restart_safe = True

        async def execute(self, inputs, ctx: CommandContext) -> Outcome:
            del inputs
            review_calls.append(ctx.step_id)
            if len(review_calls) == 1:
                return Outcome.failure(reason="token expired", label="auth_expired")
            return Outcome.success()

    class _RefreshCommand:
        kind = "DoRefresh"
        category = CommandCategory.LOCAL
        restart_safe = True

        async def execute(self, inputs, ctx: CommandContext) -> Outcome:
            del inputs
            refresh_calls.append(ctx.step_id)
            return Outcome.success()

    wf = Workflow(
        name="recovery-1",
        version=1,
        steps=(
            Step(
                id="review",
                command_kind="DoReview",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="review",
    )
    eng = _engine_with(_FailOnceCommand(), _RefreshCommand(), workflow=wf)
    exec_id = await eng.start(workflow_name="recovery-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    assert len(refresh_calls) == 1
    assert len(review_calls) == 2

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value


@pytest.mark.asyncio
async def test_recovery_policy_fires_at_most_once_per_step(db_session) -> None:
    """Second failure with the same recovery-eligible label after recovery
    has already run falls through to Tier-3 fail — no infinite loop."""
    clear_recovery_policies()
    register_recovery_policy(failure_label="auth_expired", command_kind="DoRefresh")

    review_calls: list[str] = []
    refresh_calls: list[str] = []

    class _AlwaysFail:
        kind = "DoReview"
        category = CommandCategory.LOCAL
        restart_safe = True

        async def execute(self, inputs, ctx: CommandContext) -> Outcome:
            del inputs, ctx
            review_calls.append("x")
            return Outcome.failure(reason="still expired", label="auth_expired")

    class _RefreshOk:
        kind = "DoRefresh"
        category = CommandCategory.LOCAL
        restart_safe = True

        async def execute(self, inputs, ctx: CommandContext) -> Outcome:
            del inputs, ctx
            refresh_calls.append("x")
            return Outcome.success()

    wf = Workflow(
        name="recovery-2",
        version=1,
        steps=(Step(id="review", command_kind="DoReview"),),
        entry_step_id="review",
    )
    eng = _engine_with(_AlwaysFail(), _RefreshOk(), workflow=wf)
    exec_id = await eng.start(workflow_name="recovery-2", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    assert len(refresh_calls) == 1
    assert len(review_calls) == 2
    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.FAILED.value


@pytest.mark.asyncio
async def test_ticket_payload_resolved_in_step_inputs(db_session) -> None:
    """`$ticket.<field>` resolves from the payload stashed at engine.start()
    time. Lets workflow definitions pass ticket fields into Local commands
    without each body re-fetching from the DB."""
    captured: list[dict[str, Any]] = []

    class _CaptureInputs:
        kind = "CaptureInputs"
        category = CommandCategory.LOCAL
        restart_safe = True

        async def execute(self, inputs, ctx: CommandContext) -> Outcome:
            del ctx
            captured.append(dict(inputs))
            return Outcome.success()

    wf = Workflow(
        name="payload-1",
        version=1,
        steps=(
            Step(
                id="capture",
                command_kind="CaptureInputs",
                inputs={
                    "sha": "$ticket.head_sha",
                    "missing": "$ticket.does_not_exist",
                    "from_step": "$ticket.author",
                },
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="capture",
    )
    eng = _engine_with(_CaptureInputs(), workflow=wf)
    exec_id = await eng.start(
        workflow_name="payload-1",
        ticket_id=_ticket_id(),
        ticket_payload={"head_sha": "deadbeef", "author": "alice"},
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    assert len(captured) == 1
    assert captured[0]["sha"] == "deadbeef"
    assert captured[0]["from_step"] == "alice"
    assert captured[0]["missing"] is None

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value


@pytest.mark.asyncio
async def test_ticket_payload_missing_when_not_supplied(db_session) -> None:
    """Engine.start without ticket_payload → $ticket.<field> resolves to None."""
    captured: list[Any] = []

    class _CaptureValue:
        kind = "CaptureValue"
        category = CommandCategory.LOCAL
        restart_safe = True

        async def execute(self, inputs, ctx: CommandContext) -> Outcome:
            del ctx
            captured.append(inputs.get("v"))
            return Outcome.success()

    wf = Workflow(
        name="payload-2",
        version=1,
        steps=(
            Step(
                id="cap",
                command_kind="CaptureValue",
                inputs={"v": "$ticket.head_sha"},
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="cap",
    )
    eng = _engine_with(_CaptureValue(), workflow=wf)
    await eng.start(workflow_name="payload-2", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    assert captured == [None]
