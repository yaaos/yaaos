"""WorkflowEngine register + start coverage. With the three task bodies
stubbed, these tests assert the engine writes a `workflow_executions` row
and enqueues an initial `route_workflow` task via the outbox."""

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
    # otel_trace_context stores the workflow.run span's own traceparent (not the
    # caller's), so it differs from the input "00-aabb-ccdd-01" but is still a
    # well-formed W3C traceparent string. When no OTel SDK is active the span is
    # INVALID and current_traceparent() returns None — accept both.
    stored = row.otel_trace_context
    if stored is not None:
        assert stored.startswith("00-"), f"otel_trace_context is not a valid traceparent: {stored!r}"
        assert stored != "00-aabb-ccdd-01", (
            "otel_trace_context must store the workflow.run span's traceparent, not the caller's"
        )

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


# ── New typed-column service tests ────────────────────────────────────────


class _FailOnceInputs(BaseModel):
    pass


class _FailOnce:
    """Fails the first call; succeeds all subsequent calls."""

    kind = "FailOnce"
    category = CommandCategory.LOCAL
    restart_safe = True
    _fired: bool = False

    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        if not self._fired:
            self._fired = True
            return Outcome.failure(reason="transient")
        return Outcome.success()


class _CleanupCommand:
    """Finalizer step that always succeeds."""

    kind = "Cleanup"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


@pytest.mark.asyncio
@pytest.mark.service
async def test_engine_state_persists_to_columns_service(db_session) -> None:
    """Run a workflow with a retry step and a finalizer. After completing,
    assert: (a) the six typed columns hold the correct values, (b) step_state
    contains NO engine-internal __ keys (only step-output buckets)."""
    import app.core.workflow.service as svc  # noqa: PLC0415

    fail_once = _FailOnce()
    cleanup = _CleanupCommand()

    wf = Workflow(
        name="columns-test",
        version=1,
        steps=(
            Step(
                id="work",
                command_kind="FailOnce",
                retry_policy=__import__("app.core.workflow.types", fromlist=["RetryPolicy"]).RetryPolicy(
                    max_attempts=2
                ),
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
            Step(
                id="cleanup",
                command_kind="Cleanup",
                transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
            ),
        ),
        entry_step_id="work",
        finalizer_step_id="cleanup",
    )

    eng = WorkflowEngine()
    eng.register_command(fail_once)
    eng.register_command(cleanup)
    eng.register_workflow(wf)
    svc._engine = eng

    exec_id = await eng.start(
        workflow_name="columns-test",
        ticket_id=str(uuid4()),
        session=db_session,
        ticket_payload={"org_id": str(uuid4()), "repo": "owner/repo"},
    )
    await db_session.commit()
    await _drain_via_broker(db_session)

    row = (
        await db_session.execute(select(WorkflowExecutionRow).where(WorkflowExecutionRow.id == exec_id))
    ).scalar_one()

    # Workflow should complete DONE: work fails once (retry attempt 1 succeeds),
    # cleanup never fires (no terminal-fail).
    assert row.state == WorkflowState.DONE.value

    # step_attempts column: work step attempt was incremented from 0 to 1 before retry.
    assert row.step_attempts.get("work") is not None

    # workflow_input column carries the ticket_payload dict.
    assert isinstance(row.workflow_input, dict)
    assert row.workflow_input.get("repo") == "owner/repo"

    # finalizer_fired stays False — the normal path doesn't trigger it.
    assert row.finalizer_fired is False

    # No engine-internal __ keys should remain in step_state.
    engine_keys = [k for k in (row.step_state or {}) if k.startswith("__") and k.endswith("__")]
    assert engine_keys == [], f"Unexpected engine keys in step_state: {engine_keys}"


@pytest.mark.asyncio
@pytest.mark.service
async def test_migration_backfill_from_step_state_service(db_session) -> None:
    """Seed a workflow_executions row with the old JSONB magic keys, run the
    backfill+strip SQL from migration b3c4d5e6f7a8, and assert the typed
    columns are populated and the keys are stripped from step_state."""
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    from app.core.workflow.models import WorkflowExecutionRow  # noqa: PLC0415

    old_step_state = {
        "__finalizer_fired__": True,
        "__attempts__": {"provision": 2},
        "__recovered_steps__": {"provision": "auth_expired"},
        "__append_queue__": [],
        "__appended_pool__": {},
        "__after_append__": {"step_id": "review"},
        "__ticket_payload__": {"org_id": "test-org", "repo": "x/y"},
        "__pending_failure_step__": "provision",
        "__pending_failure_reason__": "agent_failure",
        "provision": {"outputs": {"workspace_id": "ws-1"}},
    }

    row = WorkflowExecutionRow(
        ticket_id=uuid4(),
        workflow_name="old-format",
        workflow_version=1,
        state="failed",
        step_state=old_step_state,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.commit()

    # Run the migration backfill SQL (same SQL as in migration b3c4d5e6f7a8).
    await db_session.execute(
        sa_text("""
UPDATE workflow_executions
SET
    finalizer_fired = COALESCE(
        (step_state->>'__finalizer_fired__')::boolean,
        FALSE
    ),
    step_attempts = COALESCE(
        step_state->'__attempts__',
        '{}'::jsonb
    ),
    recovered_steps = COALESCE(
        step_state->'__recovered_steps__',
        '{}'::jsonb
    ),
    pending_failure_step_id = step_state->>'__pending_failure_step__',
    pending_failure_reason   = step_state->>'__pending_failure_reason__',
    workflow_input           = step_state->'__ticket_payload__'
WHERE step_state <> '{}'::jsonb
""")
    )

    # Run the strip SQL.
    await db_session.execute(
        sa_text("""
UPDATE workflow_executions
SET step_state =
    step_state
    - '__finalizer_fired__'
    - '__attempts__'
    - '__recovered_steps__'
    - '__append_queue__'
    - '__appended_pool__'
    - '__after_append__'
    - '__ticket_payload__'
    - '__pending_failure_step__'
    - '__pending_failure_reason__'
WHERE step_state <> '{}'::jsonb
""")
    )
    await db_session.commit()

    await db_session.refresh(row)
    assert row.finalizer_fired is True
    assert row.step_attempts == {"provision": 2}
    assert row.recovered_steps == {"provision": "auth_expired"}
    assert row.pending_failure_step_id == "provision"
    assert row.pending_failure_reason == "agent_failure"
    assert isinstance(row.workflow_input, dict)
    assert row.workflow_input.get("repo") == "x/y"

    # Only step-output keys survive in step_state.
    assert row.step_state == {"provision": {"outputs": {"workspace_id": "ws-1"}}}
    engine_keys = [k for k in row.step_state if k.startswith("__") and k.endswith("__")]
    assert engine_keys == []
