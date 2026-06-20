"""Service tests: workflow.command.<Kind> spans carry workflow.step_id and
workflow.attempt on both the Local and Workspace branches.

These are load-bearing regression guards for the Change-1 refactor that drops
the intermediate `workflow.start_step` custom span.  Before the change, those
attributes lived on the dropped `workflow.start_step` span.  After the change,
they must appear on the `workflow.command.<Kind>` span instead so no information
is lost.

Two tests:

- `test_local_command_span_carries_step_id_and_attempt` — Local branch via
  `_safe_execute`.  The `workflow.command.<Kind>` span must carry
  `workflow.step_id` and `workflow.attempt` as attributes.

- `test_workspace_command_span_carries_step_id_and_attempt` — Workspace branch
  in `_start_step_impl`.  Same requirement.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowState,
    step,
)
from app.core.workflow.models import WorkflowExecutionRow
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


# ── Test commands ─────────────────────────────────────────────────────────


class _SimpleLocal:
    kind = "StepAttrLocalCmd"
    category = CommandCategory.LOCAL
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


class _SimpleWs:
    kind = "StepAttrWsCmd"
    category = CommandCategory.WORKSPACE
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx) -> Outcome:  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs: Empty, ctx, *, session) -> UUID:  # type: ignore[no-untyped-def]
        del inputs, ctx, session
        return uuid4()


# ── Tests ─────────────────────────────────────────────────────────────────


async def test_local_command_span_carries_step_id_and_attempt(db_session) -> None:  # type: ignore[no-untyped-def]
    """workflow.command.<Kind> span on the Local branch carries workflow.step_id
    and workflow.attempt attributes."""
    local_step = step(_SimpleLocal)
    wf = Workflow(
        name="step-attr-local-test",
        version=1,
        steps=(local_step,),
        entry=local_step,
        transitions={local_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )

    with span_capture() as exporter:
        with scoped_engine() as eng:
            eng.register_workflow(wf)
            wfx_id = await eng.start(
                workflow_name="step-attr-local-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, wfx_id)
    assert wfx is not None
    assert wfx.state == WorkflowState.DONE.value

    spans = exporter.get_finished_spans()
    cmd_spans = [s for s in spans if s.name == "workflow.command.StepAttrLocalCmd"]
    assert cmd_spans, f"expected workflow.command.StepAttrLocalCmd span; got {[s.name for s in spans]}"
    cmd_span = cmd_spans[0]
    attrs = cmd_span.attributes or {}

    assert attrs.get("workflow.step_id") == "StepAttrLocalCmd", (
        f"expected workflow.step_id='StepAttrLocalCmd', got {attrs.get('workflow.step_id')!r}"
    )
    assert attrs.get("workflow.attempt") == 0, (
        f"expected workflow.attempt=0, got {attrs.get('workflow.attempt')!r}"
    )


async def test_workspace_command_span_carries_step_id_and_attempt(db_session) -> None:  # type: ignore[no-untyped-def]
    """workflow.command.<Kind> span on the Workspace branch carries workflow.step_id
    and workflow.attempt attributes."""
    cmd = _SimpleWs()
    ws_step = step(_SimpleWs)
    wf = Workflow(
        name="step-attr-ws-test",
        version=1,
        steps=(ws_step,),
        entry=ws_step,
        transitions={ws_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )

    with span_capture() as exporter:
        with scoped_engine() as eng:
            eng.register_command(cmd)
            eng.register_workflow(wf)
            wfx_id = await eng.start(
                workflow_name="step-attr-ws-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None
    assert wfx.state == WorkflowState.AWAITING_AGENT.value

    spans = exporter.get_finished_spans()
    cmd_spans = [s for s in spans if s.name == "workflow.command.StepAttrWsCmd"]
    assert cmd_spans, f"expected workflow.command.StepAttrWsCmd span; got {[s.name for s in spans]}"
    cmd_span = cmd_spans[0]
    attrs = cmd_span.attributes or {}

    assert attrs.get("workflow.step_id") == "StepAttrWsCmd", (
        f"expected workflow.step_id='StepAttrWsCmd', got {attrs.get('workflow.step_id')!r}"
    )
    assert attrs.get("workflow.attempt") == 0, (
        f"expected workflow.attempt=0, got {attrs.get('workflow.attempt')!r}"
    )
