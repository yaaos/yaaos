"""Service test: `engine.start()` emits a `workflow.run.<name>` root span.

Assertions:
- One `workflow.run.pr_review_v1` span is finished after `engine.start()`.
- The span's parent matches the upstream `traceparent` fixture (same trace_id
  and parent_span_id points at the fixture's span_id).
- The span carries `workflow.name`, `workflow.execution_id`, `workflow.version`
  attributes.
- The persisted `workflow_executions.otel_trace_context` row stores the
  `workflow.run` span's own traceparent — NOT the caller's upstream traceparent.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from opentelemetry import trace

from app.core.observability import current_traceparent
from app.core.workflow import (
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    step,
)
from app.core.workflow.models import WorkflowExecutionRow
from app.testing.observability import span_capture
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


class _NoopLocal:
    kind = "RunRootSpanNoop"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx, *, session=None) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        return Outcome.success()


_noop_step = step(_NoopLocal)


async def test_workflow_run_root_span_emitted(db_session) -> None:  # type: ignore[no-untyped-def]
    """engine.start() emits a `workflow.run.pr_review_v1` span parented to
    the caller's traceparent and closes it before returning."""
    wf = Workflow(
        name="pr_review_v1",
        version=1,
        steps=(_noop_step,),
        entry=_noop_step,
        transitions={_noop_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )

    tracer = trace.get_tracer("test.run_root_span")
    with span_capture() as exporter:
        with tracer.start_as_current_span("upstream-intake") as upstream_span:
            upstream_trace_id = upstream_span.get_span_context().trace_id
            upstream_span_id = upstream_span.get_span_context().span_id
            upstream_traceparent = current_traceparent()

            with scoped_engine() as eng:
                eng.register_workflow(wf)
                wfx_id = await eng.start(
                    workflow_name="pr_review_v1",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_traceparent,
                    session=db_session,
                )
                await db_session.commit()

    spans = exporter.get_finished_spans()
    run_spans = [s for s in spans if s.name == "workflow.run.pr_review_v1"]
    assert run_spans, f"expected a 'workflow.run.pr_review_v1' span; got {[s.name for s in spans]}"
    run_span = run_spans[0]

    # Span shares the upstream trace_id.
    assert run_span.context.trace_id == upstream_trace_id, (
        f"run span trace_id {run_span.context.trace_id:032x} != upstream {upstream_trace_id:032x}"
    )
    # Parent span_id is the upstream span.
    assert run_span.parent is not None, "run span must have a parent context"
    assert run_span.parent.span_id == upstream_span_id, (
        f"run span parent {run_span.parent.span_id:016x} != upstream span {upstream_span_id:016x}"
    )

    # Attributes present.
    attrs = run_span.attributes or {}
    assert attrs.get("workflow.name") == "pr_review_v1", f"missing workflow.name: {attrs}"
    assert attrs.get("workflow.version") == 1, f"missing workflow.version: {attrs}"
    assert "workflow.execution_id" in attrs, f"missing workflow.execution_id: {attrs}"
    assert attrs["workflow.execution_id"] == wfx_id, (
        f"workflow.execution_id {attrs['workflow.execution_id']!r} != {wfx_id!r}"
    )

    # The span is finished (closed) — engine.start() does not hold it open.
    assert run_span.end_time is not None, "run span must be closed when engine.start() returns"


async def test_workflow_run_span_traceparent_stored_on_row(db_session) -> None:  # type: ignore[no-untyped-def]
    """`workflow_executions.otel_trace_context` stores the `workflow.run` span's
    own traceparent — not the upstream caller's traceparent. The first
    `route_workflow` task uses this value as its parent, placing task-body spans
    one level below the run span, not at the same level as the intake request."""
    wf2 = Workflow(
        name="pr_review_v1",
        version=2,
        steps=(_noop_step,),
        entry=_noop_step,
        transitions={_noop_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )

    tracer = trace.get_tracer("test.run_root_span_stored")
    with span_capture() as exporter:
        with tracer.start_as_current_span("upstream-intake"):
            upstream_traceparent = current_traceparent()

            with scoped_engine() as eng:
                eng.register_workflow(wf2)
                wfx_id2 = await eng.start(
                    workflow_name="pr_review_v1",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_traceparent,
                    session=db_session,
                )
                await db_session.commit()

    # Load the persisted row.
    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id2))
    stored_tp = wfx.otel_trace_context

    # The stored traceparent must NOT equal the upstream caller's traceparent.
    assert stored_tp != upstream_traceparent, (
        "otel_trace_context must store the workflow.run span's traceparent, "
        f"not the caller's; got {stored_tp!r} == upstream {upstream_traceparent!r}"
    )

    # The stored traceparent must belong to the workflow.run span.
    run_spans = [s for s in exporter.get_finished_spans() if s.name == "workflow.run.pr_review_v1"]
    assert run_spans, "expected workflow.run.pr_review_v1 span"
    run_span = run_spans[0]
    expected_span_id_hex = f"{run_span.context.span_id:016x}"
    assert expected_span_id_hex in (stored_tp or ""), (
        f"stored traceparent {stored_tp!r} does not contain run span id {expected_span_id_hex}"
    )
