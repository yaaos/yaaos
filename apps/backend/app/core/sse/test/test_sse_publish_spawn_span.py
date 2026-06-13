"""Service test: SSE publish after workflow state transition emits a
`spawn:sse.publish_general` span sharing the calling span's trace_id.

Regression guard for routing the after-commit SSE publish through `spawn()`
instead of raw `asyncio.create_task`.  The change makes the publish visible
in the calling request's trace.

Test: drive a workflow state transition that enqueues a
`workflow_state_changed` SSE event, then capture spans and assert a span
named `spawn:sse.publish_general` exists and shares the trace_id of the
outer span.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from opentelemetry import trace

from app.core.observability import current_traceparent
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowState,
    get_execution_summary,
)
from app.testing.observability import span_capture
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


# ── Drain helper ──────────────────────────────────────────────────────────


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


class _QuickLocal:
    kind = "SpawnSpanTestCmd"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


async def test_sse_publish_emits_spawn_span_in_workflow_trace(db_session) -> None:  # type: ignore[no-untyped-def]
    """A workflow state transition triggers an SSE publish routed through
    spawn().  The resulting `spawn:sse.publish_general` span must share the
    trace_id of the outer span that started the workflow."""
    cmd = _QuickLocal()
    wf = Workflow(
        name="sse-spawn-span-test",
        version=1,
        steps=(
            Step(
                id="step",
                command_kind="SpawnSpanTestCmd",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="step",
    )

    tracer = trace.get_tracer("test.sse.spawn")
    with span_capture() as exporter:
        with tracer.start_as_current_span("outer-request") as outer_span:
            upstream_trace_id = outer_span.get_span_context().trace_id
            upstream_tp = current_traceparent()

            with scoped_engine() as eng:
                eng.register_command(cmd)
                eng.register_workflow(wf)
                wfx_id = await eng.start(
                    workflow_name="sse-spawn-span-test",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_tp,
                    session=db_session,
                )
                await db_session.commit()
                await _drain(db_session)

    summary = await get_execution_summary(UUID(wfx_id), session=db_session)
    assert summary is not None
    assert summary.state == WorkflowState.DONE.value

    spans = exporter.get_finished_spans()
    spawn_spans = [s for s in spans if s.name == "spawn:sse.publish_general"]
    assert spawn_spans, (
        f"expected at least one 'spawn:sse.publish_general' span; got {[s.name for s in spans]}"
    )
    for sp in spawn_spans:
        assert sp.context.trace_id == upstream_trace_id, (
            f"spawn:sse.publish_general trace_id {sp.context.trace_id:032x} != "
            f"outer trace_id {upstream_trace_id:032x}; "
            "spawn() must propagate the calling context"
        )
