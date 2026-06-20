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
- Cancellation during `awaiting_agent`: cancel + event → cancelled.
- Stale event handling: duplicate event is a no-op.
- Typed lambda inputs: upstream step outputs propagate to downstream steps.
- workflow_input: typed snapshot available to all step input lambdas.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    HANDLE_AGENT_EVENT,
    ROUTE_WORKFLOW,
    CommandCategory,
    CommandContext,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowEngine,
    WorkflowState,
    register_recovery_policy,
    request_cancel,
    resume_hitl,
    step,
    workflow_input,
)
from app.core.workflow.models import PendingHumanDecisionRow, WorkflowExecutionRow
from app.core.workflow.recovery import _clear_recovery_policies_for_tests
from app.core.workspace import WorkspaceRegistry, bind_workspace_registry, register_workspace_provider

# ── Command factory helpers ──────────────────────────────────────────────
# Phase 3 requires `kind` as a CLASS attribute (not constructor arg).
# These factories produce concrete subclasses with class-level `kind`.


def _recording(kind: str, outputs: dict[str, Any] | None = None):
    """Return (CommandClass, calls_list) for a recording LOCAL command.

    The CommandClass has `kind` as a class attribute and a `calls` list
    (also a class attribute) that records each `execute` invocation.
    Pre-register the returned instance via `register_command` so the engine
    uses YOUR instance, not an auto-discovered one.
    """
    _calls: list[dict] = []
    _out = outputs or {}

    class _Cmd:
        Inputs = Empty
        Outputs = Empty
        category = CommandCategory.LOCAL
        calls: list = _calls

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            _calls.append(
                {
                    "inputs": inputs.model_dump(),
                    "step_id": ctx.step_id,
                    "attempt": ctx.attempt,
                }
            )
            return Outcome.success(outputs=_out)

    _Cmd.kind = kind  # class attribute — readable via getattr(_Cmd, "kind")
    return _Cmd, _calls


def _failing(kind: str):
    """Return (CommandClass, instance) for a LOCAL command that always returns failure."""

    class _Cmd:
        Inputs = Empty
        Outputs = Empty
        category = CommandCategory.LOCAL
        calls: int = 0

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            del inputs, ctx
            type(self).calls += 1
            return Outcome.failure(reason="planned-failure")

    _Cmd.kind = kind
    return _Cmd, _Cmd()


def _raising(kind: str) -> type:
    """Return CommandClass for a LOCAL command that always raises RuntimeError."""

    class _Cmd:
        Inputs = Empty
        Outputs = Empty
        category = CommandCategory.LOCAL

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            del inputs, ctx
            raise RuntimeError("unexpected")

    _Cmd.kind = kind
    return _Cmd


# ── Concrete test commands that need class-level kind ────────────────────


class _HitlAsk:
    kind = "AskHuman"
    category = CommandCategory.HITL
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.hitl_pending(question={"prompt": "approve?"})


class _WorkspaceStub:
    """Workspace-category command exercised by the engine's Workspace branch.

    `dispatch` returns a fresh UUID without writing an agent_commands row;
    these tests inject the terminal AgentEvent directly via `HANDLE_AGENT_EVENT`
    (bypassing `record_agent_event` and therefore the column lookup), so a
    durable row is not needed.
    """

    kind = "DoOnAgent"
    category = CommandCategory.WORKSPACE
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs: BaseModel, ctx: CommandContext, *, session: Any) -> Any:
        del inputs, ctx, session
        return uuid4()


class _MinimalWorkspaceProvider:
    """Minimal WorkspaceProvider stub so `list_workspace_providers()` returns
    exactly one entry when Workspace commands are dispatched in tests."""

    plugin_id = "test_stub"

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {}

    async def destroy(self) -> None:  # type: ignore[no-untyped-def]
        return None

    async def health_check(self) -> None:  # type: ignore[no-untyped-def]
        return None

    async def run_coding_agent_cli(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, path, content):  # type: ignore[no-untyped-def]
        return None


# ── Drain helper ────────────────────────────────────────────────────────


async def _drain_workflow_outbox(db_session, *, max_iterations: int = 50) -> int:
    """Pull `taskiq_enqueue` rows out of the outbox and re-dispatch them
    into the matching task body via the broker's task registry. Loops until
    the outbox is empty or `max_iterations` hit (a runaway loop is a bug)."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        if decorated is None:
            raise RuntimeError(f"no registered task body for {payload['task_name']}")
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


# ── Engine fixture ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_engine():  # type: ignore[no-untyped-def]
    import app.core.workflow.service as svc  # noqa: PLC0415

    prior = svc._engine
    svc._engine = None
    yield
    svc._engine = prior


@pytest.fixture
def _with_stub_workspace_provider():
    """Register exactly one workspace provider so Workspace-step dispatch
    in `start_step` passes the single-provider guard."""
    bind_workspace_registry(WorkspaceRegistry())
    register_workspace_provider(_MinimalWorkspaceProvider())
    yield
    bind_workspace_registry(WorkspaceRegistry())


def _engine_with(*commands: Any, workflow: Workflow) -> WorkflowEngine:
    """Build an engine, pre-registering each command instance so auto-discovery
    uses the provided instance rather than auto-instantiating the class."""
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
    _CmdA, calls_a = _recording("A", outputs={"out_a": 1})
    _CmdB, calls_b = _recording("B", outputs={"out_b": 2})
    a_step = step(_CmdA)
    b_step = step(_CmdB)
    wf = Workflow(
        name="local-2",
        version=1,
        steps=(a_step, b_step),
        entry=a_step,
        transitions={
            a_step: {"success": b_step},
            b_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
        },
    )
    eng = _engine_with(_CmdA(), _CmdB(), workflow=wf)
    exec_id = await eng.start(workflow_name="local-2", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()

    await _drain_workflow_outbox(db_session)

    wfx = (
        await db_session.execute(select(WorkflowExecutionRow).where(WorkflowExecutionRow.id == exec_id))
    ).scalar_one()
    assert wfx.state == WorkflowState.DONE.value
    assert wfx.current_step_id == "B"
    assert wfx.step_state["A"]["outputs"] == {"out_a": 1}
    assert wfx.step_state["B"]["outputs"] == {"out_b": 2}
    assert len(calls_a) == 1
    assert len(calls_b) == 1


@pytest.mark.asyncio
async def test_failure_no_retry_routes_to_fail_workflow(db_session) -> None:
    _CmdBad, fail_instance = _failing("Bad")
    bad_step = step(_CmdBad)
    wf = Workflow(
        name="bad-1",
        version=1,
        steps=(bad_step,),
        entry=bad_step,
    )
    eng = _engine_with(fail_instance, workflow=wf)
    exec_id = await eng.start(workflow_name="bad-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.FAILED.value
    assert _CmdBad.calls == 1  # default retry_policy.max_attempts = 1 → no retry
    _CmdBad.calls = 0  # reset class-level counter


@pytest.mark.asyncio
async def test_failure_with_retry_eventually_fails(db_session) -> None:
    from app.core.workflow import RetryPolicy  # noqa: PLC0415

    _CmdBad, fail_instance = _failing("BadRetry")
    bad_step = step(_CmdBad, retry_policy=RetryPolicy(max_attempts=3))
    wf = Workflow(
        name="retry-3",
        version=1,
        steps=(bad_step,),
        entry=bad_step,
    )
    eng = _engine_with(fail_instance, workflow=wf)
    exec_id = await eng.start(workflow_name="retry-3", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.FAILED.value
    assert _CmdBad.calls == 3
    _CmdBad.calls = 0


@pytest.mark.asyncio
async def test_raising_command_becomes_failure(db_session) -> None:
    _CmdBoom = _raising("Boom")
    boom_step = step(_CmdBoom)
    wf = Workflow(
        name="raise-1",
        version=1,
        steps=(boom_step,),
        entry=boom_step,
    )
    eng = _engine_with(_CmdBoom(), workflow=wf)
    exec_id = await eng.start(workflow_name="raise-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.FAILED.value


@pytest.mark.asyncio
async def test_typed_lambda_inputs_resolution(db_session) -> None:
    """Upstream step outputs propagate to downstream steps via typed lambda inputs."""

    class _WorkspaceIdOut(BaseModel):
        workspace_id: str

    class _ReviewIn(BaseModel):
        workspace_id: str

    class _ProvisionCmd:
        kind = "Provision"
        Inputs = Empty
        Outputs = _WorkspaceIdOut
        category = CommandCategory.LOCAL
        calls: ClassVar[list] = []

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            type(self).calls.append(ctx.step_id)
            return Outcome.success(outputs=_WorkspaceIdOut(workspace_id="ws-123"))

    class _ReviewCmd:
        kind = "Review"
        Inputs = _ReviewIn
        Outputs = Empty
        category = CommandCategory.LOCAL
        received_inputs: ClassVar[list] = []

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            type(self).received_inputs.append(inputs)  # type: ignore[arg-type]
            return Outcome.success()

    provision_step = step(_ProvisionCmd)
    review_step = step(
        _ReviewCmd,
        inputs=lambda: _ReviewIn(workspace_id=provision_step.outputs.workspace_id),  # type: ignore[attr-defined]
    )
    wf = Workflow(
        name="lambda-resolve-1",
        version=1,
        steps=(provision_step, review_step),
        entry=provision_step,
        transitions={
            provision_step: {"success": review_step},
            review_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
        },
    )
    eng = _engine_with(workflow=wf)
    exec_id = await eng.start(workflow_name="lambda-resolve-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value
    assert len(_ReviewCmd.received_inputs) == 1
    assert _ReviewCmd.received_inputs[0].workspace_id == "ws-123"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_typed_workflow_input_lambda(db_session) -> None:
    """workflow_input snapshot is accessible inside step input lambdas."""

    class _TicketSnap(BaseModel):
        head_sha: str
        author: str | None = None

    class _CaptureInputs:
        kind = "CaptureIn"
        Inputs: type[BaseModel]
        Outputs = Empty
        category = CommandCategory.LOCAL
        received: ClassVar[list] = []

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            type(self).received.append(inputs)
            return Outcome.success()

    class _CaptureIn(BaseModel):
        sha: str
        author: str | None

    _CaptureInputs.Inputs = _CaptureIn
    ticket = workflow_input(_TicketSnap)
    cap_step = step(
        _CaptureInputs,
        inputs=lambda: _CaptureIn(  # type: ignore[misc]
            sha=ticket.outputs.head_sha,  # type: ignore[attr-defined]
            author=ticket.outputs.author,  # type: ignore[attr-defined]
        ),
    )
    wf = Workflow(
        name="wf-input-lambda-1",
        version=1,
        steps=(cap_step,),
        entry=cap_step,
        workflow_input=ticket,
        transitions={cap_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )
    eng = _engine_with(workflow=wf)
    snapshot = _TicketSnap(head_sha="deadbeef", author="alice")
    exec_id = await eng.start(
        workflow_name="wf-input-lambda-1",
        ticket_id=_ticket_id(),
        session=db_session,
        workflow_input=snapshot,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.DONE.value
    assert len(_CaptureInputs.received) == 1
    received = _CaptureInputs.received[0]
    assert received.sha == "deadbeef"  # type: ignore[attr-defined]
    assert received.author == "alice"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_workspace_step_transitions_to_awaiting_agent(
    db_session, _with_stub_workspace_provider
) -> None:
    """Workspace branch always dispatches over the wire to the single
    registered provider and parks the workflow in awaiting_agent."""
    ws = _WorkspaceStub()
    ws_step = step(_WorkspaceStub)
    wf = Workflow(
        name="ws-1",
        version=1,
        steps=(ws_step,),
        entry=ws_step,
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-1",
        ticket_id=_ticket_id(),
        session=db_session,
    )
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    assert wfx.state == WorkflowState.AWAITING_AGENT.value
    assert wfx.pending_agent_command_id is not None
    assert wfx.current_step_id == "DoOnAgent"


@pytest.mark.asyncio
async def test_handle_agent_event_advances_workflow(db_session, _with_stub_workspace_provider) -> None:
    ws = _WorkspaceStub()
    ws_step = step(_WorkspaceStub)
    wf = Workflow(
        name="ws-then-done",
        version=1,
        steps=(ws_step,),
        entry=ws_step,
        transitions={ws_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-then-done",
        ticket_id=_ticket_id(),
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
    assert wfx.step_state["DoOnAgent"]["outputs"] == {"result": "ok"}


@pytest.mark.asyncio
async def test_stale_handle_agent_event_is_noop(db_session, _with_stub_workspace_provider) -> None:
    ws = _WorkspaceStub()
    ws_step = step(_WorkspaceStub)
    wf = Workflow(
        name="ws-stale",
        version=1,
        steps=(ws_step,),
        entry=ws_step,
        transitions={ws_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-stale",
        ticket_id=_ticket_id(),
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
    _TailCls, _ = _recording("Tail")
    asker = _HitlAsk()
    tail = _TailCls()

    ask_step = step(_HitlAsk)
    tail_step = step(_TailCls)
    wf = Workflow(
        name="hitl-1",
        version=1,
        steps=(ask_step, tail_step),
        entry=ask_step,
        transitions={
            ask_step: {"hitl_pending": tail_step},
            tail_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
        },
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
async def test_request_cancel_during_awaiting_agent_then_event(
    db_session, _with_stub_workspace_provider
) -> None:
    ws = _WorkspaceStub()
    ws_step = step(_WorkspaceStub)
    wf = Workflow(
        name="ws-cancel",
        version=1,
        steps=(ws_step,),
        entry=ws_step,
        transitions={ws_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )
    eng = _engine_with(ws, workflow=wf)
    exec_id = await eng.start(
        workflow_name="ws-cancel",
        ticket_id=_ticket_id(),
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

    _NoopCls, _ = _recording("Noop2")
    noop_step = step(_NoopCls)
    wf = Workflow(
        name="term-skip",
        version=1,
        steps=(noop_step,),
        entry=noop_step,
        transitions={noop_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )
    eng = _engine_with(_NoopCls(), workflow=wf)
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
            "completed_step_id": "Noop2",
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
    _clear_recovery_policies_for_tests()
    register_recovery_policy(failure_label="auth_expired", command_kind="DoRefresh")

    review_calls: list[str] = []
    refresh_calls: list[str] = []

    class _FailOnceCommand:
        kind = "DoReview"
        category = CommandCategory.LOCAL
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            del inputs
            review_calls.append(ctx.step_id)
            if len(review_calls) == 1:
                return Outcome.failure(reason="token expired", label="auth_expired")
            return Outcome.success()

    class _RefreshCommand:
        kind = "DoRefresh"
        category = CommandCategory.LOCAL
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            del inputs
            refresh_calls.append(ctx.step_id)
            return Outcome.success()

    review_step = step(_FailOnceCommand)
    wf = Workflow(
        name="recovery-1",
        version=1,
        steps=(review_step,),
        entry=review_step,
        transitions={review_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
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
    _clear_recovery_policies_for_tests()
    register_recovery_policy(failure_label="auth_expired", command_kind="DoRefresh")

    review_calls: list[str] = []
    refresh_calls: list[str] = []

    class _AlwaysFail:
        kind = "DoReview"
        category = CommandCategory.LOCAL
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            del inputs, ctx
            review_calls.append("x")
            return Outcome.failure(reason="still expired", label="auth_expired")

    class _RefreshOk:
        kind = "DoRefresh"
        category = CommandCategory.LOCAL
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
            del inputs, ctx
            refresh_calls.append("x")
            return Outcome.success()

    review_step = step(_AlwaysFail)
    wf = Workflow(
        name="recovery-2",
        version=1,
        steps=(review_step,),
        entry=review_step,
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
async def test_finalizer_runs_then_workflow_records_failed(db_session) -> None:
    """A workflow with a declared finalizer step must end in FAILED (not DONE)
    when an earlier step fails.

    The finalizer step (cleanup) has ``transitions={"success": COMPLETE_WORKFLOW}``
    on the normal happy path.  When it runs as the failure-path finalizer it
    must NOT flip the execution to DONE — the pending failure context from the
    failing step must win instead.
    """
    _FailWork, fail_instance = _failing("Work")
    _CleanupCls, cleanup_calls = _recording("Cleanup")
    work_step = step(_FailWork)
    cleanup_step = step(_CleanupCls)
    wf = Workflow(
        name="finalizer-1",
        version=1,
        steps=(work_step, cleanup_step),
        entry=work_step,
        finalizer=cleanup_step,
        transitions={
            cleanup_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
        },
    )
    eng = _engine_with(fail_instance, _CleanupCls(), workflow=wf)
    exec_id = await eng.start(workflow_name="finalizer-1", ticket_id=_ticket_id(), session=db_session)
    await db_session.commit()
    await _drain_workflow_outbox(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, exec_id)
    # The finalizer must have run exactly once.
    assert len(cleanup_calls) == 1
    # The workflow must end in FAILED, not DONE.
    assert wfx.state == WorkflowState.FAILED.value
    assert wfx.failure_reason == "planned-failure"
    _FailWork.calls = 0  # reset class-level counter
