"""Service tests: cancel pathway + `workflow.cancelled` audit kind.

Three tests:

- `test_cancel_running_workflow_routes_through_finalizer_service` — cancel while
  AWAITING_AGENT; the agent's terminal event triggers route_workflow which routes
  through the finalizer; workflow ends CANCELLED; `workflow.cancelled` audit row
  written; on_terminal called with terminal_state=CANCELLED, failure_reason=None.

- `test_cancel_awaiting_agent_waits_for_event_service` — cancel while
  AWAITING_AGENT; assert no proactive route_workflow enqueue (request_cancel does
  not enqueue for AWAITING_AGENT); agent terminal event drives the cancel+finalizer
  pathway; workflow ends CANCELLED.

- `test_cancel_after_finalizer_discriminator_service` — cancel_requested is the
  sole discriminator between CANCELLED and FAILED after the finalizer completes.
  Two sub-workflows: one with cancel before finalizer (→ CANCELLED) and one with
  failure + no cancel (→ FAILED).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid7

import pytest

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    CleanupWorkspaceCommand,
    enqueue_command,
    record_agent_event,
)
from app.core.audit_log import list_for_entity
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    AgentDispatchCommand,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowState,
    step,
)
from app.core.workflow.models import WorkflowExecutionRow
from app.core.workflow.service import request_cancel
from app.testing.workflow_harness import set_engine_for_tests

pytestmark = pytest.mark.service

# Audit rows written by the engine's task bodies have no OrgContextMiddleware in
# tests, so `_workflow_org_id` falls back to UUID(int=0). Use this sentinel for
# all `list_for_entity` calls.
_NIL_ORG = UUID(int=0)


# ── Reusable command stubs ────────────────────────────────────────────────────


class _DispatchingWs(AgentDispatchCommand):
    """AgentDispatchCommand that enqueues a real agent_commands row and records
    the returned command_id for inspection. Uses class attributes for org_id so
    the engine can auto-instantiate via `_DispatchingWs()`. Tests override
    class attributes before calling register_workflow."""

    kind = "CancelTestDispatch"
    Inputs = Empty
    Outputs = Empty
    _org_id: UUID = UUID("00000000-0000-0000-0000-000000000000")
    dispatched_command_id: UUID | None = None

    async def execute(self, inputs: Empty, ctx: Any) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs: Empty, ctx: Any, *, session: Any) -> UUID:  # type: ignore[no-untyped-def]
        del inputs
        command_id = uuid7()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=uuid4(),
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=type(self)._org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        type(self).dispatched_command_id = command_id
        return command_id


class _FinalizerWs(AgentDispatchCommand):
    """AgentDispatchCommand used as the finalizer (cleanup) step.

    Tracks dispatch count so tests can confirm it ran exactly once.
    """

    kind = "CancelTestFinalizer"
    Inputs = Empty
    Outputs = Empty
    _org_id: UUID = UUID("00000000-0000-0000-0000-000000000000")
    dispatched_command_id: UUID | None = None
    call_count: int = 0

    async def execute(self, inputs: Empty, ctx: Any) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs: Empty, ctx: Any, *, session: Any) -> UUID:  # type: ignore[no-untyped-def]
        del inputs
        type(self).call_count += 1
        command_id = uuid7()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=uuid4(),
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=type(self)._org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        type(self).dispatched_command_id = command_id
        return command_id


class _LocalFinalizer:
    """LocalCommand finalizer — executes inline (no agent round-trip)."""

    kind = "CancelTestLocalFinalizer"
    Inputs = Empty
    Outputs = Empty
    call_count: int = 0

    async def execute(self, inputs: Empty, ctx: Any, *, session: Any = None) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        type(self).call_count += 1
        return Outcome.success()


# ── Drain helper ──────────────────────────────────────────────────────────────


async def _drain(db_session: Any, *, max_iters: int = 50) -> None:
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:  # type: ignore[no-untyped-def]
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None, f"no task body for {payload['task_name']}"
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_running_workflow_routes_through_finalizer_service(
    db_session: Any,
) -> None:
    """Acceptance test: cancel a workflow while AWAITING_AGENT (main step dispatched).

    Steps:
    1. Start workflow → AWAITING_AGENT (main step dispatched).
    2. request_cancel → AWAITING_AGENT → no proactive enqueue.
    3. Agent sends terminal event → handle_agent_event → route_workflow.
    4. route_workflow sees cancel_requested=TRUE, finalizer_fired=FALSE → routes
       through finalizer (sets finalizer_fired=TRUE, enqueues start_step).
    5. Finalizer's agent command is dispatched → agent sends success event.
    6. route_workflow: finalizer_fired=TRUE, completed_step_id=finalizer.step_id,
       cancel_requested=TRUE → CANCELLED.

    Assertions:
    - Finalizer step ran (call_count = 1).
    - Workflow state == CANCELLED.
    - audit_log row with kind="workflow.cancelled" exists.
    - on_terminal callback received terminal_state=CANCELLED, failure_reason=None.
    """
    _FinalizerWs.call_count = 0
    _FinalizerWs.dispatched_command_id = None
    _DispatchingWs.dispatched_command_id = None

    org_id = uuid4()
    _DispatchingWs._org_id = org_id
    _FinalizerWs._org_id = org_id

    terminal_calls: list[dict] = []

    async def _on_terminal(**kwargs: Any) -> None:
        terminal_calls.append(dict(kwargs))

    main_step = step(_DispatchingWs)
    finalizer_step = step(_FinalizerWs)

    workflow = Workflow(
        name="cancel-through-finalizer-test",
        version=1,
        steps=(main_step, finalizer_step),
        entry=main_step,
        finalizer=finalizer_step,
        transitions={
            main_step: {
                "success": TerminalAction.COMPLETE_WORKFLOW,
                "failure": TerminalAction.FAIL_WORKFLOW,
            },
            finalizer_step: {"success": TerminalAction.FAIL_WORKFLOW},
        },
        on_terminal=_on_terminal,
    )

    ticket_id = str(uuid4())

    with set_engine_for_tests() as eng:
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="cancel-through-finalizer-test",
            ticket_id=ticket_id,
            session=db_session,
        )
        await db_session.commit()

        # Drain start_step → AWAITING_AGENT (main step dispatched).
        await _drain(db_session)

        wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value
        assert _DispatchingWs.dispatched_command_id is not None

        # Cancel while AWAITING_AGENT — no proactive enqueue (flag set only).
        cancelled = await request_cancel(wfx_id, session=db_session)
        assert cancelled is True
        await db_session.commit()

        # No proactive route_workflow enqueued for AWAITING_AGENT.
        pending_after_cancel = await get_pending_task_names(db_session)
        assert not pending_after_cancel, (
            f"expected no pending tasks after cancel in AWAITING_AGENT; got {pending_after_cancel}"
        )

        # Agent sends a terminal event (success — the cancel_requested flag,
        # not the event outcome, drives the CANCELLED path).
        main_event = AgentEvent(
            command_id=_DispatchingWs.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_SUCCESS,
            outcome_label="success",
            outputs={},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        await record_agent_event(main_event, session=db_session)
        await db_session.commit()

        # Drain: handle_agent_event → route_workflow (sees cancel → routes to
        # finalizer) → start_step(finalizer) → finalizer dispatched.
        await _drain(db_session)

        # Finalizer's agent command was enqueued.
        assert _FinalizerWs.call_count == 1, (
            f"finalizer dispatch should have been called once; got {_FinalizerWs.call_count}"
        )
        assert _FinalizerWs.dispatched_command_id is not None

        # Send the finalizer's success event.
        fin_event = AgentEvent(
            command_id=_FinalizerWs.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_SUCCESS,
            outcome_label="success",
            outputs={},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        await record_agent_event(fin_event, session=db_session)
        await db_session.commit()

        # Drain: handle_agent_event → route_workflow → CANCELLED.
        await _drain(db_session)

    # Verify final state.
    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None
    assert wfx.state == WorkflowState.CANCELLED.value, f"expected CANCELLED; got {wfx.state}"

    # Verify audit row.
    # Task bodies have no OrgContextMiddleware in tests; `_workflow_org_id`
    # falls back to UUID(int=0) — use that sentinel for the query.
    entries = await list_for_entity("workflow_execution", UUID(wfx_id), org_id=_NIL_ORG)
    cancelled_entries = [e for e in entries if e.kind == "workflow.cancelled"]
    assert len(cancelled_entries) == 1, (
        f"expected 1 workflow.cancelled audit row; got {len(cancelled_entries)}"
    )
    payload = cancelled_entries[0].payload
    assert payload["workflow_execution_id"] == wfx_id
    assert payload["ticket_id"] == ticket_id

    # Verify on_terminal callback received CANCELLED + failure_reason=None.
    assert len(terminal_calls) == 1, f"expected 1 on_terminal call; got {len(terminal_calls)}"
    call = terminal_calls[0]
    assert call["terminal_state"] == WorkflowState.CANCELLED, (
        f"expected CANCELLED; got {call['terminal_state']}"
    )
    assert call["failure_reason"] is None, (
        f"expected failure_reason=None on cancel; got {call['failure_reason']}"
    )


@pytest.mark.asyncio
async def test_cancel_awaiting_agent_waits_for_event_service(
    db_session: Any,
) -> None:
    """Cancel while AWAITING_AGENT: no proactive enqueue. The agent's terminal
    event (failure in this case) drives handle_agent_event → route_workflow which
    sees cancel_requested and routes through the (local) finalizer. Workflow ends
    CANCELLED.
    """
    _LocalFinalizer.call_count = 0
    _DispatchingWs.dispatched_command_id = None

    org_id = uuid4()
    _DispatchingWs._org_id = org_id

    main_step = step(_DispatchingWs)
    fin_step = step(_LocalFinalizer)

    workflow = Workflow(
        name="cancel-awaiting-agent-test",
        version=1,
        steps=(main_step, fin_step),
        entry=main_step,
        finalizer=fin_step,
        transitions={
            main_step: {
                "success": TerminalAction.COMPLETE_WORKFLOW,
                "failure": TerminalAction.FAIL_WORKFLOW,
            },
            fin_step: {"success": TerminalAction.FAIL_WORKFLOW},
        },
    )

    with set_engine_for_tests() as eng:
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="cancel-awaiting-agent-test",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()
        await _drain(db_session)

        wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value

        # Cancel while AWAITING_AGENT — must NOT enqueue route_workflow.
        cancelled = await request_cancel(wfx_id, session=db_session)
        assert cancelled is True
        await db_session.commit()

        pending = await get_pending_task_names(db_session)
        assert not pending, f"request_cancel must not enqueue tasks for AWAITING_AGENT; got {pending}"

        # Agent sends a failure event. handle_agent_event → route_workflow →
        # cancel detected → finalizer (local, runs inline) → CANCELLED.
        assert _DispatchingWs.dispatched_command_id is not None
        fail_event = AgentEvent(
            command_id=_DispatchingWs.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_FAILURE,
            outcome_label="failure",
            outputs={},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        await record_agent_event(fail_event, session=db_session)
        await db_session.commit()

        await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None
    assert wfx.state == WorkflowState.CANCELLED.value, f"expected CANCELLED; got {wfx.state}"
    # Local finalizer ran exactly once.
    assert _LocalFinalizer.call_count == 1, (
        f"expected finalizer call_count=1; got {_LocalFinalizer.call_count}"
    )

    # Audit row must exist.
    entries = await list_for_entity("workflow_execution", UUID(wfx_id), org_id=_NIL_ORG)
    assert any(e.kind == "workflow.cancelled" for e in entries), "expected workflow.cancelled audit row"


@pytest.mark.asyncio
async def test_cancel_after_finalizer_discriminator_service(
    db_session: Any,
) -> None:
    """cancel_requested is the SOLE discriminator between CANCELLED and FAILED
    after the finalizer completes.

    Scenario A: main step FAILS, cancel_requested=TRUE → workflow enters CANCELLED
    (not FAILED), despite the finalizer's transition being FAIL_WORKFLOW.

    Scenario B: main step FAILS, cancel_requested=FALSE → workflow enters FAILED
    (not CANCELLED).

    Both use the same workflow shape with a local finalizer.
    """
    # ── Scenario A: failure + cancel → CANCELLED ─────────────────────────────

    class _FailLocal:
        """LocalCommand that always fails."""

        kind = "CancelDiscriminatorFail"
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: Empty, ctx: Any, *, session: Any = None) -> Outcome:  # type: ignore[no-untyped-def]
            del inputs, ctx, session
            return Outcome.failure(reason="deliberate_failure")

    class _LocalFinalizerA:
        """LocalCommand finalizer for scenario A."""

        kind = "CancelDiscriminatorFinalizer"
        Inputs = Empty
        Outputs = Empty
        call_count: int = 0

        async def execute(self, inputs: Empty, ctx: Any, *, session: Any = None) -> Outcome:  # type: ignore[no-untyped-def]
            del inputs, ctx, session
            type(self).call_count += 1
            return Outcome.success()

    _LocalFinalizerA.call_count = 0

    fail_step_a = step(_FailLocal)
    fin_step_a = step(_LocalFinalizerA)

    wf_a = Workflow(
        name="cancel-discriminator-a",
        version=1,
        steps=(fail_step_a, fin_step_a),
        entry=fail_step_a,
        finalizer=fin_step_a,
        transitions={
            fail_step_a: {"failure": TerminalAction.FAIL_WORKFLOW},
            fin_step_a: {"success": TerminalAction.FAIL_WORKFLOW},
        },
    )

    with set_engine_for_tests() as eng_a:
        eng_a.register_workflow(wf_a)
        wfx_id_a = await eng_a.start(
            workflow_name="cancel-discriminator-a",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()
        # Cancel BEFORE any drain so start_step sees cancel_requested=TRUE and
        # routes through the finalizer instead of executing the fail step.
        await request_cancel(wfx_id_a, session=db_session)
        await db_session.commit()
        # Drain fully: start_step sees cancel_requested → routes to finalizer
        # (one-shot) → finalizer runs → route_workflow → CANCELLED.
        await _drain(db_session)

    wfx_a = await db_session.get(WorkflowExecutionRow, UUID(wfx_id_a))
    assert wfx_a is not None
    assert wfx_a.state == WorkflowState.CANCELLED.value, (
        f"scenario A (cancel+fail) expected CANCELLED; got {wfx_a.state}"
    )
    entries_a = await list_for_entity("workflow_execution", UUID(wfx_id_a), org_id=_NIL_ORG)
    assert any(e.kind == "workflow.cancelled" for e in entries_a), (
        "scenario A: expected workflow.cancelled audit row"
    )
    assert not any(e.kind == "workflow.failed" for e in entries_a), (
        "scenario A: expected NO workflow.failed audit row"
    )
    assert _LocalFinalizerA.call_count >= 1, "scenario A: finalizer should have run"

    # ── Scenario B: failure, no cancel → FAILED ───────────────────────────────

    class _FailLocalB:
        """LocalCommand that always fails — scenario B variant (distinct kind)."""

        kind = "CancelDiscriminatorFailB"
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: Empty, ctx: Any, *, session: Any = None) -> Outcome:  # type: ignore[no-untyped-def]
            del inputs, ctx, session
            return Outcome.failure(reason="deliberate_failure_b")

    class _LocalFinalizerB:
        """LocalCommand finalizer for scenario B — distinct kind."""

        kind = "CancelDiscriminatorFinalizerB"
        Inputs = Empty
        Outputs = Empty
        call_count: int = 0

        async def execute(self, inputs: Empty, ctx: Any, *, session: Any = None) -> Outcome:  # type: ignore[no-untyped-def]
            del inputs, ctx, session
            type(self).call_count += 1
            return Outcome.success()

    _LocalFinalizerB.call_count = 0

    fail_step_b = step(_FailLocalB)
    fin_step_b = step(_LocalFinalizerB)

    wf_b = Workflow(
        name="cancel-discriminator-b",
        version=1,
        steps=(fail_step_b, fin_step_b),
        entry=fail_step_b,
        finalizer=fin_step_b,
        transitions={
            fail_step_b: {"failure": TerminalAction.FAIL_WORKFLOW},
            fin_step_b: {"success": TerminalAction.FAIL_WORKFLOW},
        },
    )

    with set_engine_for_tests() as eng_b:
        eng_b.register_workflow(wf_b)
        wfx_id_b = await eng_b.start(
            workflow_name="cancel-discriminator-b",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()
        # Drain fully without cancelling — failure triggers finalizer, then FAILED.
        await _drain(db_session)

    wfx_b = await db_session.get(WorkflowExecutionRow, UUID(wfx_id_b))
    assert wfx_b is not None
    assert wfx_b.state == WorkflowState.FAILED.value, (
        f"scenario B (fail, no cancel) expected FAILED; got {wfx_b.state}"
    )
    entries_b = await list_for_entity("workflow_execution", UUID(wfx_id_b), org_id=_NIL_ORG)
    assert any(e.kind == "workflow.failed" for e in entries_b), (
        "scenario B: expected workflow.failed audit row"
    )
    assert not any(e.kind == "workflow.cancelled" for e in entries_b), (
        "scenario B: expected NO workflow.cancelled audit row"
    )
    assert _LocalFinalizerB.call_count == 1, "scenario B: finalizer should have run exactly once"
