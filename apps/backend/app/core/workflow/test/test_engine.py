"""WorkflowEngine register + start coverage. The three task bodies stay
stubbed in Phase 1 (foundations); these tests assert the engine writes a
`workflow_executions` row and enqueues an initial `route_workflow` task
via the outbox."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.core.tasks import drain_once
from app.core.workflow import (
    CommandCategory,
    CommandContext,
    CommandNotRegisteredError,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowCommand,
    WorkflowEngine,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowState,
)
from app.core.workflow.models import WorkflowExecutionRow

# ── A throwaway WorkflowCommand for registration tests ──────────────────


class _NoopInputs(BaseModel):
    pass


class _NoopCommand:
    kind = "Noop"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


# Protocol structural check.
assert isinstance(_NoopCommand(), WorkflowCommand)


def _engine_with_workflow(name: str = "demo") -> WorkflowEngine:
    eng = WorkflowEngine()
    eng.register_command(_NoopCommand())
    wf = Workflow(
        name=name,
        version=1,
        steps=(
            Step(
                id="only",
                command_kind="Noop",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="only",
    )
    eng.register_workflow(wf)
    return eng


def test_register_workflow_with_unregistered_command_kind_ok_until_start() -> None:
    """Forward references are allowed at register time; start() validates."""
    eng = WorkflowEngine()
    wf = Workflow(
        name="x",
        version=1,
        steps=(Step(id="s", command_kind="Missing"),),
        entry_step_id="s",
    )
    eng.register_workflow(wf)  # no raise


def test_register_workflow_rejects_unknown_entry_step() -> None:
    eng = WorkflowEngine()
    with pytest.raises(WorkflowError):
        eng.register_workflow(
            Workflow(name="x", version=1, steps=(Step(id="s", command_kind="K"),), entry_step_id="not-s")
        )


def test_register_workflow_rejects_transition_to_unknown_step() -> None:
    eng = WorkflowEngine()
    with pytest.raises(WorkflowError):
        eng.register_workflow(
            Workflow(
                name="x",
                version=1,
                steps=(Step(id="a", command_kind="K", transitions={"success": "ghost"}),),
                entry_step_id="a",
            )
        )


def test_double_register_command_raises() -> None:
    eng = WorkflowEngine()
    eng.register_command(_NoopCommand())
    with pytest.raises(WorkflowError):
        eng.register_command(_NoopCommand())


def test_get_workflow_picks_latest_version_when_unspecified() -> None:
    eng = WorkflowEngine()
    eng.register_command(_NoopCommand())
    for v in (1, 2, 3):
        eng.register_workflow(
            Workflow(
                name="multi",
                version=v,
                steps=(Step(id="s", command_kind="Noop"),),
                entry_step_id="s",
            )
        )
    assert eng.get_workflow("multi").version == 3
    assert eng.get_workflow("multi", version=1).version == 1


def test_get_workflow_unknown_raises() -> None:
    eng = WorkflowEngine()
    with pytest.raises(WorkflowNotFoundError):
        eng.get_workflow("ghost")


async def _drain_via_broker(db_session) -> None:
    """Drive outbox rows through their registered task bodies via the broker,
    without reaching into outbox internals beyond the private submodule."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(50):
        n = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if n == 0:
            break


@pytest.mark.asyncio
async def test_start_creates_execution_row_and_routes_to_done(db_session) -> None:
    """Engine.start writes a workflow_executions row; draining the outbox
    advances the single-step workflow to DONE — proving the initial
    route_workflow task was enqueued correctly."""
    import app.core.workflow.service as svc  # noqa: PLC0415

    eng = _engine_with_workflow()
    # Install as process singleton so task bodies (route_workflow, start_step)
    # can look up the engine via get_engine().
    svc._engine = eng

    ticket_id = str(uuid4())
    exec_id = await eng.start(
        workflow_name="demo",
        ticket_id=ticket_id,
        session=db_session,
        traceparent="00-aabb-ccdd-01",
    )
    await db_session.commit()

    row = (
        await db_session.execute(select(WorkflowExecutionRow).where(WorkflowExecutionRow.id == exec_id))
    ).scalar_one()
    assert row.state == WorkflowState.PENDING.value
    assert row.workflow_name == "demo"
    assert row.workflow_version == 1
    assert row.current_step_id is None
    assert row.pending_agent_command_id is None
    assert row.step_state == {}
    assert row.otel_trace_context == "00-aabb-ccdd-01"

    # Draining proves the initial route_workflow task was enqueued and the
    # single-step workflow completes.
    await _drain_via_broker(db_session)

    row = (
        await db_session.execute(select(WorkflowExecutionRow).where(WorkflowExecutionRow.id == exec_id))
    ).scalar_one()
    assert row.state == WorkflowState.DONE.value


@pytest.mark.asyncio
async def test_start_unknown_command_kind_raises_before_writing(db_session) -> None:
    """If a workflow step references an unregistered command, start()
    must fail loud — and must NOT leave a workflow_executions row."""
    eng = WorkflowEngine()
    eng.register_workflow(
        Workflow(
            name="bad",
            version=1,
            steps=(Step(id="s", command_kind="Missing"),),
            entry_step_id="s",
        )
    )

    with pytest.raises(CommandNotRegisteredError):
        await eng.start(workflow_name="bad", ticket_id=str(uuid4()), session=db_session)

    rows = (await db_session.execute(select(WorkflowExecutionRow))).scalars().all()
    assert rows == []
