"""Service tests: `_start_step_impl` emits a `workflow.command.<Kind>` span
for Workspace commands — mirrors the span `_safe_execute` already opens for
Local/HITL commands.

The `workflow.start_step` custom span no longer exists.  The taskiq
auto-span (`task:workflow.start_step`) is the task boundary; the
`workflow.command.<Kind>` span is its direct child.

Three tests:

- `test_workspace_command_span_emitted` — a Workspace command that dispatches
  successfully → a `workflow.command.ProvisionWs` span exists; the
  `CommandContext.traceparent` that reached `dispatch()` matches the command
  span's own traceparent.

- `test_workspace_command_span_error_on_dispatch_raise` — dispatch raises →
  command span is `StatusCode.ERROR` with an `exception` event.

- `test_local_command_span_category_attribute` — regression guard; the Local
  path still emits `workflow.command.<Kind>` with a `command.category` attribute
  equal to `"local"`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.core.observability import current_traceparent
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowState,
)
from app.core.workflow.models import WorkflowExecutionRow
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


# ── Test Workspace commands ────────────────────────────────────────────


class _RecordingWs:
    """Workspace command that records the CommandContext it received in dispatch().

    On success it returns a fake command_id (UUID). The test inspects
    `received_ctx` to confirm the traceparent was the command span's own."""

    kind = "ProvisionWs"
    category = CommandCategory.WORKSPACE
    restart_safe = True
    received_ctx = None  # populated by dispatch()
    received_traceparent: str | None = None  # traceparent active inside dispatch()

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs, ctx, *, session):  # type: ignore[no-untyped-def]
        del inputs, session
        _RecordingWs.received_ctx = ctx
        # Also snapshot the currently-active OTel span traceparent so the test
        # can compare it to what the command span should have produced.
        _RecordingWs.received_traceparent = current_traceparent()
        return uuid4()


class _RaisingWs:
    """Workspace command whose dispatch() raises — tests the error path."""

    kind = "ProvisionWsRaises"
    category = CommandCategory.WORKSPACE
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs, ctx, *, session):  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        raise RuntimeError("workspace-boom")


class _SucceedingLocal:
    kind = "SpanCategoryLocalCmd"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


# ── Tests ──────────────────────────────────────────────────────────────


async def test_workspace_command_span_emitted(db_session) -> None:  # type: ignore[no-untyped-def]
    """A Workspace command dispatch emits `workflow.command.ProvisionWs`.
    The `CommandContext.traceparent` that arrived at `dispatch()` is the
    command span's own traceparent."""
    _RecordingWs.received_ctx = None
    _RecordingWs.received_traceparent = None

    ws_cmd = _RecordingWs()
    wf = Workflow(
        name="ws-span-test",
        version=1,
        steps=(
            Step(
                id="step",
                command_kind="ProvisionWs",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="step",
    )

    outer_tracer = trace.get_tracer("test.ws_span")
    with span_capture() as exporter:
        with outer_tracer.start_as_current_span("upstream-intake"):
            upstream_tp = current_traceparent()

            with scoped_engine() as eng:
                eng.register_command(ws_cmd)
                eng.register_workflow(wf)
                wfx_id = await eng.start(
                    workflow_name="ws-span-test",
                    ticket_id=str(uuid4()),
                    traceparent=upstream_tp,
                    session=db_session,
                )
                await db_session.commit()
                # Only drain start_step — the workflow parks AWAITING_AGENT after dispatch.
                await _drain(db_session)

    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]

    # (a) command span emitted
    cmd_spans = [s for s in spans if s.name == "workflow.command.ProvisionWs"]
    assert cmd_spans, f"expected workflow.command.ProvisionWs span; got {span_names}"
    cmd_span = cmd_spans[0]

    # No workflow.start_step custom span exists (taskiq auto-span is the boundary).
    start_spans = [s for s in spans if s.name == "workflow.start_step"]
    assert not start_spans, f"workflow.start_step custom span must not exist; got {start_spans}"

    # (b) ctx.traceparent that reached dispatch() is the command span's traceparent
    assert _RecordingWs.received_ctx is not None, "dispatch() was not called"
    ctx_tp = _RecordingWs.received_ctx.traceparent
    cmd_span_id_hex = f"{cmd_span.context.span_id:016x}"
    assert ctx_tp is not None, "CommandContext.traceparent must not be None inside command span"
    assert cmd_span_id_hex in ctx_tp, (
        f"ctx.traceparent {ctx_tp!r} does not contain command span id {cmd_span_id_hex}; "
        "it may still carry the outer task's traceparent"
    )

    # Sanity: workflow parked AWAITING_AGENT (dispatch succeeded, workflow waited)
    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None
    assert wfx.state == WorkflowState.AWAITING_AGENT.value, f"expected AWAITING_AGENT, got {wfx.state}"


async def test_workspace_command_span_error_on_dispatch_raise(db_session) -> None:  # type: ignore[no-untyped-def]
    """dispatch() raises → `workflow.command.ProvisionWsRaises` span is
    `StatusCode.ERROR` with an `exception` event."""
    ws_cmd = _RaisingWs()
    wf = Workflow(
        name="ws-span-raise-test",
        version=1,
        steps=(
            Step(
                id="step",
                command_kind="ProvisionWsRaises",
                transitions={"failure": TerminalAction.FAIL_WORKFLOW},
            ),
        ),
        entry_step_id="step",
    )

    with span_capture() as exporter:
        with scoped_engine() as eng:
            eng.register_command(ws_cmd)
            eng.register_workflow(wf)
            wfx_id = await eng.start(
                workflow_name="ws-span-raise-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    spans = exporter.get_finished_spans()
    span_names = [s.name for s in spans]

    # Command span is ERROR
    cmd_spans = [s for s in spans if s.name == "workflow.command.ProvisionWsRaises"]
    assert cmd_spans, f"expected workflow.command.ProvisionWsRaises span; got {span_names}"
    cmd_span = cmd_spans[0]
    assert cmd_span.status.status_code == StatusCode.ERROR, (
        f"command span expected ERROR, got {cmd_span.status.status_code}"
    )
    exception_events = [e for e in cmd_span.events if e.name == "exception"]
    assert exception_events, "expected an 'exception' event on the command span"

    # No workflow.start_step custom span exists.
    start_spans = [s for s in spans if s.name == "workflow.start_step"]
    assert not start_spans, f"workflow.start_step custom span must not exist; got {start_spans}"

    # Workflow recorded as FAILED
    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None
    assert wfx.state == WorkflowState.FAILED.value, f"expected FAILED, got {wfx.state}"


async def test_local_command_span_category_attribute(db_session) -> None:  # type: ignore[no-untyped-def]
    """Regression guard: Local path still emits `workflow.command.<Kind>` and
    the span carries `command.category="local"` attribute."""
    cmd = _SucceedingLocal()
    wf = Workflow(
        name="local-category-attr-test",
        version=1,
        steps=(
            Step(
                id="step",
                command_kind="SpanCategoryLocalCmd",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="step",
    )

    with span_capture() as exporter:
        with scoped_engine() as eng:
            eng.register_command(cmd)
            eng.register_workflow(wf)
            await eng.start(
                workflow_name="local-category-attr-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    spans = exporter.get_finished_spans()
    cmd_spans = [s for s in spans if s.name == "workflow.command.SpanCategoryLocalCmd"]
    assert cmd_spans, f"expected workflow.command.SpanCategoryLocalCmd; got {[s.name for s in spans]}"
    cmd_span = cmd_spans[0]
    attrs = cmd_span.attributes or {}
    assert attrs.get("command.category") == "local", (
        f"expected command.category='local', got {attrs.get('command.category')!r}"
    )
