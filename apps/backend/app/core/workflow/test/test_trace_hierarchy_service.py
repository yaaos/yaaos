"""Service tests: workflow.start_step spans are always children of workflow.run.<name>.

The bug: after a Workspace command parks in AWAITING_AGENT and the agent
posts a terminal event, `handle_agent_event` enqueues `route_workflow` with
the agent's HTTP-request traceparent.  Every subsequent `start_step` then
hangs off the agent's request span instead of `workflow.run.<name>`.

The fix: task bodies read `wfx.otel_trace_context` from the DB row and use
that as the parent — never the caller-supplied `traceparent` arg, which may
be stale or from a different trace.

Two tests:

- `test_workflow_trace_hierarchy_after_workspace_command` — Workspace step
  followed by a Local terminal step.  Injects the terminal event with a
  *different* traceparent (simulating the agent's own HTTP request context).
  Asserts both `workflow.start_step` spans are children of `workflow.run.*`.

- `test_workflow_trace_hierarchy_pure_local` — two Local steps, no agent
  hop.  All start_step spans must still be children of `workflow.run.*`.
  Regression pin for the pre-existing case that already worked.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from opentelemetry import trace

from app.core.observability import current_traceparent
from app.core.tasks import drain_once, enqueue, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowState,
)
from app.core.workflow.models import WorkflowExecutionRow
from app.core.workflow.service import HANDLE_AGENT_EVENT
from app.testing.observability import span_capture
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


# ── Shared drain helper ────────────────────────────────────────────────


async def _drain(db_session, *, max_iters: int = 50) -> None:  # type: ignore[no-untyped-def]
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:  # type: ignore[no-untyped-def]
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return


# ── Command stubs ──────────────────────────────────────────────────────


class _MinimalWs:
    """Workspace command that parks AWAITING_AGENT with a synthetic command_id."""

    kind = "HierarchyTestWs"
    category = CommandCategory.WORKSPACE
    restart_safe = True
    dispatched_id: UUID | None = None

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs, ctx, *, session):  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        _MinimalWs.dispatched_id = uuid4()
        return _MinimalWs.dispatched_id


class _TerminalLocal:
    kind = "HierarchyTestTerminal"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


class _NoopLocal:
    kind = "HierarchyTestNoop"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


# ── Tests ──────────────────────────────────────────────────────────────


async def test_workflow_trace_hierarchy_after_workspace_command(db_session) -> None:  # type: ignore[no-untyped-def]
    """All workflow.start_step spans are children of workflow.run.* even when
    handle_agent_event is triggered with a *different* traceparent (the agent's
    own HTTP request context).

    Failure mode before the fix: the second start_step (terminal local step)
    is a child of the agent-request span, not workflow.run.*."""
    _MinimalWs.dispatched_id = None

    ws_cmd = _MinimalWs()
    terminal_cmd = _TerminalLocal()

    wf = Workflow(
        name="hierarchy-ws-test",
        version=1,
        steps=(
            Step(
                id="ws_step",
                command_kind="HierarchyTestWs",
                transitions={"success": "terminal"},
            ),
            Step(
                id="terminal",
                command_kind="HierarchyTestTerminal",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="ws_step",
    )

    tracer = trace.get_tracer("test.hierarchy.ws")
    with span_capture() as exporter:
        with tracer.start_as_current_span("intake-upstream") as upstream_span:
            upstream_trace_id = upstream_span.get_span_context().trace_id
            upstream_tp = current_traceparent()

            with scoped_engine() as eng:
                eng.register_command(ws_cmd)
                eng.register_command(terminal_cmd)
                eng.register_workflow(wf)

                wfx_id = await eng.start(
                    workflow_name="hierarchy-ws-test",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_tp,
                    session=db_session,
                )
                await db_session.commit()

                # Drain: route_workflow (initial) → start_step (ws_step) → AWAITING_AGENT
                await _drain(db_session)

                wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
                assert wfx is not None
                assert wfx.state == WorkflowState.AWAITING_AGENT.value, (
                    f"expected AWAITING_AGENT after ws dispatch, got {wfx.state}"
                )
                assert _MinimalWs.dispatched_id is not None

                # Inject the terminal event with a DIFFERENT traceparent —
                # simulating the agent's own HTTP-request span context, which is
                # what `core/agent_gateway` receives from the agent's event POST.
                # The bug: before the fix, route_workflow and subsequent start_step
                # would parent off this alien traceparent rather than workflow.run.*.
                agent_tracer = trace.get_tracer("test.hierarchy.agent")
                with agent_tracer.start_as_current_span("agent-http-post"):
                    alien_tp = current_traceparent()

                await enqueue(
                    HANDLE_AGENT_EVENT,
                    args={
                        "workflow_execution_id": wfx_id,
                        "agent_command_id": str(_MinimalWs.dispatched_id),
                        "outcome_label": "success",
                        "outputs": {},
                        "traceparent": alien_tp,  # intentionally wrong trace context
                    },
                    session=db_session,
                )
                await db_session.commit()

                # Drain: handle_agent_event → route_workflow → start_step (terminal)
                # → route_workflow → DONE
                await _drain(db_session)

        wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
        assert wfx is not None
        assert wfx.state == WorkflowState.DONE.value, f"expected DONE, got {wfx.state}"

    spans = exporter.get_finished_spans()

    # Locate the workflow.run span — must be the single source of truth for
    # the parent of every workflow.start_step span.
    run_spans = [s for s in spans if s.name == "workflow.run.hierarchy-ws-test"]
    assert run_spans, f"expected workflow.run.hierarchy-ws-test; got {[s.name for s in spans]}"
    run_span = run_spans[0]
    run_span_id = run_span.context.span_id

    # Both start_step spans (ws_step and terminal) must be direct children of
    # workflow.run.hierarchy-ws-test.
    start_step_spans = [s for s in spans if s.name == "workflow.start_step"]
    assert len(start_step_spans) >= 2, (
        f"expected >=2 workflow.start_step spans; got {[s.name for s in spans]}"
    )

    for ss in start_step_spans:
        assert ss.parent is not None, f"start_step span {ss} has no parent"
        assert ss.parent.span_id == run_span_id, (
            f"start_step span parent {ss.parent.span_id:016x} != "
            f"workflow.run span {run_span_id:016x}; "
            "handle_agent_event must use wfx.otel_trace_context, not the alien traceparent"
        )
        # All spans must share the upstream trace_id (no alien trace leaking in).
        assert ss.context.trace_id == upstream_trace_id, (
            f"start_step span trace_id {ss.context.trace_id:032x} != upstream {upstream_trace_id:032x}; "
            "agent's HTTP request traceparent must not leak into the workflow trace"
        )


async def test_workflow_trace_hierarchy_pure_local(db_session) -> None:  # type: ignore[no-untyped-def]
    """Regression pin: two Local steps — both start_step spans are direct
    children of workflow.run.*, sharing the upstream trace_id.

    This path already worked before the fix; this test ensures it stays green."""
    noop = _NoopLocal()

    wf = Workflow(
        name="hierarchy-local-test",
        version=1,
        steps=(
            Step(id="a", command_kind="HierarchyTestNoop", transitions={"success": "b"}),
            Step(
                id="b",
                command_kind="HierarchyTestNoop",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="a",
    )

    tracer = trace.get_tracer("test.hierarchy.local")
    with span_capture() as exporter:
        with tracer.start_as_current_span("intake-upstream") as upstream_span:
            upstream_trace_id = upstream_span.get_span_context().trace_id
            upstream_tp = current_traceparent()

            with scoped_engine() as eng:
                eng.register_command(noop)
                eng.register_workflow(wf)

                wfx_id = await eng.start(
                    workflow_name="hierarchy-local-test",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_tp,
                    session=db_session,
                )
                await db_session.commit()
                await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None
    assert wfx.state == WorkflowState.DONE.value, f"expected DONE, got {wfx.state}"

    spans = exporter.get_finished_spans()

    run_spans = [s for s in spans if s.name == "workflow.run.hierarchy-local-test"]
    assert run_spans, f"expected workflow.run.hierarchy-local-test; got {[s.name for s in spans]}"
    run_span = run_spans[0]
    run_span_id = run_span.context.span_id

    start_step_spans = [s for s in spans if s.name == "workflow.start_step"]
    assert len(start_step_spans) >= 2, (
        f"expected >=2 workflow.start_step spans; got {[s.name for s in spans]}"
    )

    for ss in start_step_spans:
        assert ss.parent is not None, "start_step span has no parent"
        assert ss.parent.span_id == run_span_id, (
            f"start_step parent {ss.parent.span_id:016x} != run span {run_span_id:016x}"
        )
        assert ss.context.trace_id == upstream_trace_id, (
            f"start_step trace_id {ss.context.trace_id:032x} != upstream {upstream_trace_id:032x}"
        )
