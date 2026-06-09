"""Service test: workflow engine's Workspace branch calls `command.dispatch`,
parks on the **returned** command_id (no synthesized stub id), and resumes when
a terminal AgentEvent arrives at `record_agent_event` — correlated via
`agent_commands.workflow_execution_id` with no workspace-row dependency.

Drives a two-step workflow (Workspace → Local terminal) via `scoped_workflow`:

1. Register a test `_DispatchingWs` Workspace command whose `dispatch` enqueues
   a real `agent_commands` row via `enqueue_command` with the correct
   `workflow_execution_id` and returns its command_id.
2. `engine.start(...)` → outbox drain runs `start_step` → parks
   `AWAITING_AGENT` with `pending_agent_command_id` == the row's id.
3. Simulate the agent's terminal event by calling `record_agent_event`
   directly. The gateway resolves the workflow from the column (not from a
   workspace row), invariant under the redesign.
4. Drain outbox → workflow advances through the Local terminal step → DONE.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    CleanupWorkspaceCommand,
    enqueue_command,
    record_agent_event,
)
from app.core.audit_log import ActorKind
from app.core.auth import org_context
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
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


class _DispatchingWs:
    """Workspace command whose `dispatch` enqueues a real agent_commands row
    pre-stamped with the workflow_execution_id. `execute()` is unused — the
    engine's Workspace branch never calls it."""

    kind = "DispatchingWs"
    category = CommandCategory.WORKSPACE
    restart_safe = True

    def __init__(self, *, org_id: UUID) -> None:
        self._org_id = org_id
        self.dispatched_command_id: UUID | None = None

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs, ctx, *, session):  # type: ignore[no-untyped-def]
        del inputs
        command_id = uuid4()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=uuid4(),  # any workspace id — gateway no longer needs the row
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=self._org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        self.dispatched_command_id = command_id
        return command_id


class _NoopLocal:
    """Local terminal step — drains the workflow to DONE after the Workspace
    step's terminal event arrives."""

    kind = "DispatchingWsTerminal"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


async def _drain(db_session, *, max_iters: int = 50) -> None:
    """Drain the outbox by re-dispatching `taskiq_enqueue` rows into the
    matching task body via the broker's task registry."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
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


async def test_workspace_dispatch_parks_on_returned_command_id_and_resumes(
    db_session,
) -> None:
    """End-to-end: `WorkflowCommand.dispatch` returns a real `agent_commands.id`,
    the engine parks on it, the terminal event resolves the workflow via
    `workflow_execution_id` (no workspace-row read), and the workflow advances.
    """
    # The engine itself is org-agnostic; the Workspace command's dispatch
    # enqueues with an org_id that is just stored on the agent_commands row.
    # A synthesized UUID is sufficient — agent_commands.org_id is not FK-bound.
    org_id = uuid4()
    ws_cmd = _DispatchingWs(org_id=org_id)
    local_cmd = _NoopLocal()

    workflow = Workflow(
        name="workspace-dispatch-service-test",
        version=1,
        steps=(
            Step(
                id="dispatch",
                command_kind="DispatchingWs",
                transitions={"success": "terminal"},
            ),
            Step(
                id="terminal",
                command_kind="DispatchingWsTerminal",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="dispatch",
    )

    with scoped_engine() as eng:
        eng.register_command(ws_cmd)
        eng.register_command(local_cmd)
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="workspace-dispatch-service-test",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()

        # Drain `start_step` → expect AWAITING_AGENT with pending_agent_command_id
        # equal to the id `dispatch` returned (the real agent_commands row PK).
        await _drain(db_session)

        wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value
        assert wfx.pending_agent_command_id is not None
        assert ws_cmd.dispatched_command_id is not None
        assert wfx.pending_agent_command_id == ws_cmd.dispatched_command_id

        # Simulate the agent's terminal event via the real ingestion path.
        # The gateway resolves the workflow purely via the agent_commands.workflow_execution_id
        # column — no workspace row is involved.
        terminal_event = AgentEvent(
            command_id=ws_cmd.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_SUCCESS,
            outcome_label="success",
            outputs={},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        # The ownership guard in `record_agent_event` asserts the command row's
        # org matches the active org context, so run inside `org_context`.
        async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
            await record_agent_event(terminal_event, session=db_session)
        await db_session.commit()

        # Drain handle_agent_event + route_workflow + start_step for terminal +
        # route_workflow to DONE.
        await _drain(db_session)

        wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
        assert wfx is not None
        assert wfx.state == WorkflowState.DONE.value
