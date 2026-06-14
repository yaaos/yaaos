"""Service tests: workflow.command.<Kind> spans are always within workflow.run.<name>'s trace.

The bug (now fixed): after a Workspace command parks in AWAITING_AGENT and
the agent posts a terminal event, `handle_agent_event` used to enqueue
`route_workflow` with the agent's HTTP-request traceparent.  Every subsequent
`start_step` then hung off the agent's request span instead of the
`workflow.run.<name>` trace.

The fix: task bodies read `wfx.otel_trace_context` from the DB row so the
engine's spans always belong to the workflow trace.

The `workflow.start_step` custom span no longer exists.  The engine asserts
trace continuity at the `workflow.command.<Kind>` level: every command span
must share the upstream `trace_id` and the run span must be reachable in the
same trace.

Three tests:

- `test_workflow_command_parents_to_workflow_run` — regression guard:
  every `workflow.command.<Kind>` span is a direct child of
  `workflow.run.<name>` (same trace_id AND parent_span_id ==
  run_span.span_id). Covers both the Local branch and the Workspace branch.

- `test_workflow_trace_hierarchy_after_workspace_command` — Workspace step
  followed by a Local terminal step.  Injects the terminal event with a
  *different* traceparent (simulating the agent's own HTTP request context).
  Asserts all `workflow.command.<Kind>` spans share the upstream trace_id.

- `test_workflow_trace_hierarchy_pure_local` — two Local steps, no agent
  hop.  All command spans must still share the upstream trace_id.
  Regression pin for the pre-existing case.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from sqlalchemy import select as sa_select

from app.core.observability import current_traceparent
from app.core.tasks import drain_once, enqueue, get_pending_outbox_payloads, get_pending_task_names
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
    """All workflow.command.<Kind> spans share the upstream trace_id even when
    handle_agent_event is triggered with a *different* traceparent (the agent's
    own HTTP request context).

    Failure mode before the fix: the second start_step would hang off the
    agent-request span instead of the workflow.run.* trace."""
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

    # workflow.run span must exist.
    run_spans = [s for s in spans if s.name == "workflow.run.hierarchy-ws-test"]
    assert run_spans, f"expected workflow.run.hierarchy-ws-test; got {[s.name for s in spans]}"

    # No workflow.start_step custom span exists (taskiq auto-span covers the boundary).
    start_step_custom = [s for s in spans if s.name == "workflow.start_step"]
    assert not start_step_custom, f"workflow.start_step custom span must not exist; got {start_step_custom}"

    # All workflow.command.<Kind> spans must share the upstream trace_id —
    # proving the alien traceparent did not bleed into the workflow trace.
    cmd_spans = [s for s in spans if s.name.startswith("workflow.command.")]
    assert len(cmd_spans) >= 2, f"expected >=2 workflow.command.* spans; got {[s.name for s in spans]}"

    for cs in cmd_spans:
        assert cs.context.trace_id == upstream_trace_id, (
            f"command span {cs.name!r} trace_id {cs.context.trace_id:032x} != "
            f"upstream {upstream_trace_id:032x}; "
            "agent's HTTP request traceparent must not leak into the workflow trace"
        )


async def test_workflow_trace_hierarchy_pure_local(db_session) -> None:  # type: ignore[no-untyped-def]
    """Regression pin: two Local steps — all workflow.command.<Kind> spans
    share the upstream trace_id.

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

    # No workflow.start_step custom span.
    start_step_custom = [s for s in spans if s.name == "workflow.start_step"]
    assert not start_step_custom, f"workflow.start_step custom span must not exist; got {start_step_custom}"

    # All workflow.command.<Kind> spans share the upstream trace_id.
    cmd_spans = [s for s in spans if s.name.startswith("workflow.command.")]
    assert len(cmd_spans) >= 2, f"expected >=2 workflow.command.* spans; got {[s.name for s in spans]}"

    for cs in cmd_spans:
        assert cs.context.trace_id == upstream_trace_id, (
            f"command span {cs.name!r} trace_id {cs.context.trace_id:032x} != "
            f"upstream {upstream_trace_id:032x}"
        )


async def test_workflow_command_parents_to_workflow_run(db_session) -> None:  # type: ignore[no-untyped-def]
    """Regression guard: every `workflow.command.<Kind>` span is a direct
    child of `workflow.run.<name>` — same trace_id AND parent_span_id equal
    to the run span's span_id. The link is made by opening
    `workflow.command.<Kind>` via `with_remote_parent_span(wfx.otel_trace_context)`.

    Covers both Local and Workspace branches because the two call sites
    differ (Local goes through `_safe_execute`; Workspace goes through
    `_start_step_impl` directly).
    """
    _MinimalWs.dispatched_id = None

    ws_cmd = _MinimalWs()
    terminal_cmd = _TerminalLocal()
    noop_cmd = _NoopLocal()

    # ── Sub-test 1: Local-only workflow ───────────────────────────────
    wf_local = Workflow(
        name="direct-parent-local",
        version=1,
        steps=(
            Step(
                id="step_a",
                command_kind="HierarchyTestNoop",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="step_a",
    )

    tracer = trace.get_tracer("test.direct.parent")
    with span_capture() as exporter:
        with tracer.start_as_current_span("upstream-local") as upstream_span:
            upstream_trace_id = upstream_span.get_span_context().trace_id
            run_span_id = None  # populated below from the captured spans
            upstream_tp = current_traceparent()

            with scoped_engine() as eng:
                eng.register_command(noop_cmd)
                eng.register_workflow(wf_local)

                await eng.start(
                    workflow_name="direct-parent-local",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_tp,
                    session=db_session,
                )
                await db_session.commit()
                await _drain(db_session)

    spans = exporter.get_finished_spans()
    run_spans = [s for s in spans if s.name == "workflow.run.direct-parent-local"]
    assert run_spans, f"Local: no workflow.run span; got {[s.name for s in spans]}"
    run_span = run_spans[0]
    run_span_id = run_span.context.span_id

    cmd_spans = [s for s in spans if s.name.startswith("workflow.command.")]
    assert cmd_spans, f"Local: no workflow.command.* span; got {[s.name for s in spans]}"
    for cs in cmd_spans:
        assert cs.context.trace_id == upstream_trace_id, (
            f"Local: command span {cs.name!r} trace_id "
            f"{cs.context.trace_id:032x} != upstream {upstream_trace_id:032x}"
        )
        assert cs.parent is not None, f"Local: command span {cs.name!r} has no parent"
        assert cs.parent.span_id == run_span_id, (
            f"Local: command span {cs.name!r} parent_span_id "
            f"{cs.parent.span_id:016x} != run span_id {run_span_id:016x}; "
            "workflow.command must be a direct child of workflow.run"
        )

    # ── Sub-test 2: Workspace workflow ────────────────────────────────
    _MinimalWs.dispatched_id = None

    wf_ws = Workflow(
        name="direct-parent-ws",
        version=1,
        steps=(
            Step(
                id="ws_step",
                command_kind="HierarchyTestWs",
                transitions={"success": "term"},
            ),
            Step(
                id="term",
                command_kind="HierarchyTestTerminal",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="ws_step",
    )

    with span_capture() as exporter2:
        with tracer.start_as_current_span("upstream-ws") as upstream_ws_span:
            upstream_ws_trace_id = upstream_ws_span.get_span_context().trace_id
            upstream_ws_tp = current_traceparent()

            with scoped_engine() as eng2:
                eng2.register_command(ws_cmd)
                eng2.register_command(terminal_cmd)
                eng2.register_workflow(wf_ws)

                wfx_id = await eng2.start(
                    workflow_name="direct-parent-ws",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_ws_tp,
                    session=db_session,
                )
                await db_session.commit()
                await _drain(db_session)

                # Park in AWAITING_AGENT — now inject terminal event
                assert _MinimalWs.dispatched_id is not None

                agent_tracer = trace.get_tracer("test.agent.http")
                with agent_tracer.start_as_current_span("agent-http-post"):
                    alien_tp = current_traceparent()

                await enqueue(
                    HANDLE_AGENT_EVENT,
                    args={
                        "workflow_execution_id": wfx_id,
                        "agent_command_id": str(_MinimalWs.dispatched_id),
                        "outcome_label": "success",
                        "outputs": {},
                        "traceparent": alien_tp,
                    },
                    session=db_session,
                )
                await db_session.commit()
                await _drain(db_session)

    spans2 = exporter2.get_finished_spans()
    run_spans2 = [s for s in spans2 if s.name == "workflow.run.direct-parent-ws"]
    assert run_spans2, f"WS: no workflow.run span; got {[s.name for s in spans2]}"
    run_span2 = run_spans2[0]
    run_span_id2 = run_span2.context.span_id

    cmd_spans2 = [s for s in spans2 if s.name.startswith("workflow.command.")]
    assert len(cmd_spans2) >= 2, f"WS: expected >=2 workflow.command.* spans; got {[s.name for s in spans2]}"
    for cs in cmd_spans2:
        assert cs.context.trace_id == upstream_ws_trace_id, (
            f"WS: command span {cs.name!r} trace_id "
            f"{cs.context.trace_id:032x} != upstream {upstream_ws_trace_id:032x}"
        )
        assert cs.parent is not None, f"WS: command span {cs.name!r} has no parent"
        assert cs.parent.span_id == run_span_id2, (
            f"WS: command span {cs.name!r} parent_span_id "
            f"{cs.parent.span_id:016x} != run span_id {run_span_id2:016x}; "
            "workflow.command must be a direct child of workflow.run"
        )


async def test_workflow_task_spans_in_workflow_trace(db_session) -> None:  # type: ignore[no-untyped-def]
    """Outbox-to-task-span traceparent pipe: enqueued `task:workflow.*`
    messages carry `wfx.otel_trace_context` in their `metadata.traceparent`
    field.

    This proves the pipe that lets `TaskSpanMiddleware.pre_execute` open
    `task:<name>` spans as children of `workflow.run.<name>`. The middleware
    extracts the traceparent and calls `restore_traceparent_context` before
    opening the span — exercised directly by
    `test_task_span_uses_metadata_traceparent_as_parent` in the tasks test
    suite. Here we verify the pipe from the workflow engine to the outbox.

    Two checkpoints:
    1. The `route_workflow` outbox entry carries a `metadata.traceparent`
       that encodes the same trace_id as `workflow.run.<name>`.
    2. After draining, the workflow reaches DONE (guards the pipe is not broken).
    """
    from app.core.tasks import TaskMetadata  # noqa: PLC0415

    noop = _NoopLocal()

    wf = Workflow(
        name="layer-b-trace-pipe",
        version=1,
        steps=(
            Step(
                id="step_x",
                command_kind="HierarchyTestNoop",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="step_x",
    )

    tracer = trace.get_tracer("test.layer_b.pipe")
    with span_capture():
        with tracer.start_as_current_span("upstream-layer-b") as upstream_span:
            upstream_trace_id = upstream_span.get_span_context().trace_id
            upstream_tp = current_traceparent()

            with scoped_engine() as eng:
                eng.register_command(noop)
                eng.register_workflow(wf)

                await eng.start(
                    workflow_name="layer-b-trace-pipe",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_tp,
                    session=db_session,
                )
                await db_session.commit()

                # Before draining: check that the first outbox entry (route_workflow)
                # carries a traceparent in the same trace as workflow.run.
                payloads = await get_pending_outbox_payloads(db_session)
                assert payloads, "expected at least one pending outbox entry before drain"
                first_meta_raw = payloads[0].get("metadata")
                assert first_meta_raw is not None, "enqueued task must carry metadata (with traceparent)"
                first_meta = TaskMetadata.model_validate(first_meta_raw)
                assert first_meta.traceparent is not None, (
                    "metadata.traceparent must be set; enqueue must auto-fill from current span"
                )
                # Extract the trace_id encoded in the traceparent.
                # Format: "00-<trace_id_hex>-<span_id_hex>-<flags>"
                parts = first_meta.traceparent.split("-")
                assert len(parts) == 4, f"malformed traceparent: {first_meta.traceparent}"
                encoded_trace_id = int(parts[1], 16)
                assert encoded_trace_id == upstream_trace_id, (
                    f"outbox traceparent trace_id {encoded_trace_id:032x} != "
                    f"upstream trace_id {upstream_trace_id:032x}; "
                    "task:workflow.* spans would land in the wrong trace"
                )

                # Drain to completion and verify DONE.
                await _drain(db_session)

    all_wfx = (
        (await db_session.execute(sa_select(WorkflowExecutionRow).order_by(WorkflowExecutionRow.id.desc())))
        .scalars()
        .all()
    )
    layer_b_wfx = [w for w in all_wfx if "layer-b" in (w.workflow_name or "")]
    assert layer_b_wfx, "expected layer-b-trace-pipe workflow execution row"
    assert layer_b_wfx[0].state == WorkflowState.DONE.value, f"expected DONE, got {layer_b_wfx[0].state}"
