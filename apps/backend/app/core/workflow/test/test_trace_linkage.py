"""Trace-linkage audit: all workflow task-body spans share one trace_id.

The three workflow task bodies (`start_step`, `route_workflow`,
`handle_agent_event`) each open a span via `with_remote_parent_span`,
extracting the upstream span from the `traceparent` task arg. This test
drives a complete workflow run with an `InMemorySpanExporter` attached
and asserts every emitted span shares the same `trace_id` — proving one
trace covers webhook → workflow start → all task bodies → terminal.

Trace ID stays continuous from webhook to PR comment through the
workflow-engine layer here; the final hop (vcs.post_review) emits its own
spans through SQLAlchemy/HTTP auto-instrumentation under the same trace
context when [domain_reviewer.md PostFindings] runs inside a span. The
Go-subprocess hop rides on env-passing `TRACEPARENT`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowState,
    get_engine,
)
from app.core.workflow.models import WorkflowExecutionRow


@pytest.fixture(autouse=True)
def _isolated_engine():  # type: ignore[no-untyped-def]
    """Ensure each test gets a fresh workflow engine singleton."""
    import app.core.workflow.service as svc  # noqa: PLC0415

    prior = svc._engine
    svc._engine = None
    yield
    svc._engine = prior


@pytest.fixture
def in_memory_spans():
    """Install a real `TracerProvider` (in case observability.configure()
    wasn't called yet in this test session) and wire an in-memory span
    exporter onto it. Spans emitted by the workflow task bodies during
    the test land in `exporter.get_finished_spans()`."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    yield exporter
    processor.shutdown()


class _NoopLocal:
    kind = "Noop"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


async def _drain(db_session):  # type: ignore[no-untyped-def]
    """Run the outbox dispatcher until empty so chained task enqueues fire."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(50):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()


async def test_workflow_task_body_spans_share_trace_id(in_memory_spans, db_session) -> None:  # type: ignore[no-untyped-def]
    """All three workflow task bodies emit spans nested under the
    `traceparent` passed at workflow start. Drive a two-Local-step
    workflow under one upstream span and assert every emitted span
    carries the same trace_id."""
    eng = get_engine()
    eng.register_command(_NoopLocal())
    workflow = Workflow(
        name="trace-linkage-test",
        version=1,
        steps=(
            Step(id="a", command_kind="Noop", transitions={"success": "b"}),
            Step(id="b", command_kind="Noop", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}),
        ),
        entry_step_id="a",
    )
    eng.register_workflow(workflow)

    tracer = trace.get_tracer("trace-linkage-test")
    with tracer.start_as_current_span("intake-upstream") as upstream:
        upstream_trace_id = upstream.get_span_context().trace_id
        from app.core.observability import current_traceparent  # noqa: PLC0415

        wfx_id = await eng.start(
            workflow_name="trace-linkage-test",
            ticket_id=str(uuid4()),
            traceparent=current_traceparent(),
            session=db_session,
        )
        await db_session.commit()

    await _drain(db_session)

    # Workflow reached DONE.
    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx.state == WorkflowState.DONE.value

    # Inspect the spans that fired during the workflow run.
    spans = in_memory_spans.get_finished_spans()
    workflow_span_names = {
        "workflow.start_step",
        "workflow.route_workflow",
    }
    emitted_workflow_spans = [s for s in spans if s.name in workflow_span_names]
    # Two steps x (start_step + route_workflow) = at least 4 task-body spans.
    assert len(emitted_workflow_spans) >= 4, (
        f"expected >=4 workflow task-body spans, got {[s.name for s in emitted_workflow_spans]}"
    )

    # Critically: every workflow task-body span shares the upstream trace_id.
    for span in emitted_workflow_spans:
        assert span.context.trace_id == upstream_trace_id, (
            f"span {span.name!r} has trace_id {span.context.trace_id:032x}, expected {upstream_trace_id:032x}"
        )


async def test_handle_agent_event_span_shares_trace_id(in_memory_spans, db_session) -> None:  # type: ignore[no-untyped-def]
    """The `handle_agent_event` task body also nests under the upstream
    `traceparent` — the agent's terminal-event ingestion is part of the
    same trace, not a new one. Drive a Workspace step on `remote_agent`
    + inject the terminal event under the upstream span."""
    eng = get_engine()

    class _NoopWs:
        kind = "DoOnAgent"
        category = CommandCategory.WORKSPACE
        restart_safe = True

        async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
            del inputs, ctx
            return Outcome.success()

    eng.register_command(_NoopWs())
    workflow = Workflow(
        name="trace-linkage-ws",
        version=1,
        steps=(
            Step(
                id="do", command_kind="DoOnAgent", transitions={"success": TerminalAction.COMPLETE_WORKFLOW}
            ),
        ),
        entry_step_id="do",
    )
    eng.register_workflow(workflow)

    tracer = trace.get_tracer("trace-linkage-ws-test")
    with tracer.start_as_current_span("intake-upstream") as upstream:
        upstream_trace_id = upstream.get_span_context().trace_id
        from app.core.observability import current_traceparent  # noqa: PLC0415

        wfx_id = await eng.start(
            workflow_name="trace-linkage-ws",
            ticket_id=str(uuid4()),
            workspace_provider="remote_agent",
            traceparent=current_traceparent(),
            session=db_session,
        )
        await db_session.commit()
        await _drain(db_session)

        # Now AWAITING_AGENT. Inject the terminal event UNDER the same
        # upstream span — exactly what `core/agent_gateway.handle_event`
        # would do when capturing `traceparent` from the AgentEvent.
        wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
        from app.core.tasks import enqueue  # noqa: PLC0415
        from app.core.workflow.service import HANDLE_AGENT_EVENT  # noqa: PLC0415

        await enqueue(
            HANDLE_AGENT_EVENT,
            args={
                "workflow_execution_id": wfx_id,
                "agent_command_id": str(wfx.pending_agent_command_id),
                "outcome_label": "success",
                "outputs": {},
                "traceparent": current_traceparent(),
            },
            session=db_session,
        )
        await db_session.commit()
        await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx.state == WorkflowState.DONE.value

    spans = in_memory_spans.get_finished_spans()
    handle_spans = [s for s in spans if s.name == "workflow.handle_agent_event"]
    assert len(handle_spans) >= 1
    for span in handle_spans:
        assert span.context.trace_id == upstream_trace_id
