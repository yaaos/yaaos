"""Trace-linkage audit: all workflow task-body spans share one trace_id.

`handle_agent_event` opens a span via `with_remote_parent_span`. `start_step`
no longer opens a custom span — taskiq's auto-emitted `task:workflow.start_step`
span covers the hop; the inner `workflow.command.<Kind>` span is its direct child.
`route_workflow` also does not open a custom span — taskiq's own
`task:workflow.route_workflow` covers it.

This test drives a complete workflow run with an `InMemorySpanExporter` and
asserts every emitted custom span shares the same `trace_id` — proving one
trace covers webhook → workflow start → all task bodies → terminal.

Trace ID stays continuous from webhook to PR comment through the
workflow-engine layer here; the final hop (`vcs.post_finding`) emits its own
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
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowState,
    get_engine,
    step,
)
from app.core.workflow.models import WorkflowExecutionRow
from app.core.workspace import WorkspaceRegistry, bind_workspace_registry, register_workspace_provider


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


class _NoopA:
    kind = "TraceLinkNoopA"
    category = CommandCategory.LOCAL
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


class _NoopB:
    kind = "TraceLinkNoopB"
    category = CommandCategory.LOCAL
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx) -> Outcome:  # type: ignore[no-untyped-def]
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
    """Workflow task bodies emit spans within the upstream trace when drained
    under the upstream span context.  Drive a two-Local-step workflow and
    assert the custom spans emitted share the upstream trace_id."""
    eng = get_engine()
    a_step = step(_NoopA)
    b_step = step(_NoopB)
    workflow = Workflow(
        name="trace-linkage-test",
        version=1,
        steps=(a_step, b_step),
        entry=a_step,
        transitions={
            a_step: {"success": b_step},
            b_step: {"success": TerminalAction.COMPLETE_WORKFLOW},
        },
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

        # Drain inside the upstream span context so command spans inherit it.
        # In production, taskiq extracts the trace context from the task message
        # envelope and installs it on the worker thread — the in-process drain
        # helper inherits whatever span context is currently active.
        await _drain(db_session)

    # Workflow reached DONE.
    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx.state == WorkflowState.DONE.value

    # Inspect the spans that fired during the workflow run.
    spans = in_memory_spans.get_finished_spans()

    # Neither route_workflow nor start_step open custom spans — both are covered
    # by taskiq auto-instrumentation.
    route_workflow_custom_spans = [s for s in spans if s.name == "workflow.route_workflow"]
    assert route_workflow_custom_spans == [], (
        f"expected no workflow.route_workflow custom span, got {route_workflow_custom_spans}"
    )
    start_step_custom = [s for s in spans if s.name == "workflow.start_step"]
    assert start_step_custom == [], f"expected no workflow.start_step custom span, got {start_step_custom}"

    # workflow.command.<Kind> spans exist for both steps.
    cmd_spans = [s for s in spans if s.name.startswith("workflow.command.")]
    assert len(cmd_spans) >= 2, f"expected >=2 workflow.command.* spans, got {[s.name for s in spans]}"

    # Every command span shares the upstream trace_id.
    for span in cmd_spans:
        assert span.context.trace_id == upstream_trace_id, (
            f"span {span.name!r} has trace_id {span.context.trace_id:032x}, expected {upstream_trace_id:032x}"
        )


async def test_handle_agent_event_span_shares_trace_id(in_memory_spans, db_session) -> None:  # type: ignore[no-untyped-def]
    """The `handle_agent_event` task body also nests under the upstream
    `traceparent` — the agent's terminal-event ingestion is part of the
    same trace, not a new one. Drive a Workspace step and inject the
    terminal event under the upstream span."""
    eng = get_engine()

    class _MinimalProvider:
        plugin_id = "trace_test_stub"

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

    bind_workspace_registry(WorkspaceRegistry())
    register_workspace_provider(_MinimalProvider())

    class _NoopWs:
        kind = "DoOnAgent"
        category = CommandCategory.WORKSPACE
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: Empty, ctx) -> Outcome:  # type: ignore[no-untyped-def]
            del inputs, ctx
            return Outcome.success()

        async def dispatch(self, inputs: Empty, ctx, *, session) -> uuid4().__class__:  # type: ignore[no-untyped-def]
            del inputs, ctx, session
            return uuid4()

    ws_step = step(_NoopWs)
    eng.register_command(_NoopWs())
    workflow = Workflow(
        name="trace-linkage-ws",
        version=1,
        steps=(ws_step,),
        entry=ws_step,
        transitions={ws_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )
    eng.register_workflow(workflow)

    tracer = trace.get_tracer("trace-linkage-ws-test")
    with tracer.start_as_current_span("intake-upstream") as upstream:
        upstream_trace_id = upstream.get_span_context().trace_id
        from app.core.observability import current_traceparent  # noqa: PLC0415

        wfx_id = await eng.start(
            workflow_name="trace-linkage-ws",
            ticket_id=str(uuid4()),
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
