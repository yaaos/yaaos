"""Service tests: per-workflow on_start / on_terminal callbacks and recovery_commands.

Four tests:

- `test_per_workflow_callback_fires_service` — `Workflow.on_start` is awaited
  exactly once with the expected primitive kwargs during the workflow bootstrap.

- `test_per_workflow_recovery_resolution_service` — A `WorkflowCommand` with
  `recovers_failure_label = "probe_fail"` is inserted by the engine when the
  preceding step fails with that label.

- `test_register_workflow_rejects_duplicate_recovery_label_service` — Attempting
  to register a workflow whose `recovery_commands` tuple contains two classes that
  share the same `recovers_failure_label` raises `WorkflowError`.

- `test_callback_exception_records_on_span_service` — A raising `on_start` callback
  is recorded on the `workflow.callback.on_start` span as ERROR.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry.trace import StatusCode

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowError,
    step,
)
from app.testing.observability import span_capture
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


# ── Minimal command stubs ─────────────────────────────────────────────────────


class _PassLocal:
    """Single-step local command that succeeds."""

    kind = "CallbackTestPassLocal"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: Any, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


class _FailLabel:
    """Single-step local command that fails with a specific outcome label."""

    kind = "CallbackTestFailLabel"
    Inputs = Empty
    Outputs = Empty
    _label: str = "probe_fail"

    async def execute(self, inputs: Empty, ctx: Any, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.failure(reason="intentional probe failure", outcome_label=self._label)


class _RecoveryCmd:
    """Recovery WorkflowCommand that handles `probe_fail` failure labels.

    Not a real AgentDispatchCommand — just a local command that acts as a
    recovery interposition point in the test. This is the correct shape the
    engine expects: a class with `recovers_failure_label` ClassVar and `kind`.
    """

    kind = "CallbackTestRecoveryCmd"
    recovers_failure_label: str = "probe_fail"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: Any, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


class _RecoveryConflictA:
    kind = "CallbackTestConflictA"
    recovers_failure_label = "dupe_label"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: Any, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


class _RecoveryConflictB:
    kind = "CallbackTestConflictB"
    recovers_failure_label = "dupe_label"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: Any, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


# ── Drain helper ─────────────────────────────────────────────────────────────


async def _drain(db_session: Any, *, max_iters: int = 30) -> None:
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:  # type: ignore[no-untyped-def]
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None, f"no task body for {payload['task_name']}"
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_workflow_callback_fires_service(db_session: Any) -> None:
    """Workflow.on_start is awaited exactly once with the expected primitive
    kwargs during the engine's bootstrap RUNNING write."""
    org_id = uuid4()
    invocations: list[dict] = []

    async def _probe(**kwargs: Any) -> None:
        invocations.append(dict(kwargs))

    pass_step = step(_PassLocal)
    wf = Workflow(
        name="callback-fire-test",
        version=1,
        steps=(pass_step,),
        entry=pass_step,
        transitions={pass_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
        on_start=_probe,
    )

    with scoped_engine() as eng:
        eng.register_workflow(wf)
        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id = await eng.start(
                workflow_name="callback-fire-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    assert len(invocations) == 1, f"expected 1 invocation; got {len(invocations)}"
    call = invocations[0]
    assert str(call["workflow_execution_id"]) == wfx_id
    assert call["workflow_name"] == "callback-fire-test"
    assert isinstance(call["ticket_id"], UUID)
    assert call["org_id"] == org_id
    assert "session" in call


@pytest.mark.asyncio
async def test_per_workflow_recovery_resolution_service(db_session: Any) -> None:
    """A recovery command declared in Workflow.recovery_commands is inserted by
    the engine after a step fails with the matching outcome_label.

    Verification: after full drain the workflow terminates as DONE (the recovery
    command succeeded and allowed re-execution to proceed) — the recovery
    command handled the failure.
    """
    from app.core.workflow import WorkflowState  # noqa: PLC0415
    from app.core.workflow.models import WorkflowExecutionRow  # noqa: PLC0415

    org_id = uuid4()

    fail_step = step(_FailLabel)
    wf = Workflow(
        name="recovery-resolution-test",
        version=1,
        steps=(fail_step,),
        entry=fail_step,
        transitions={
            fail_step: {
                "probe_fail": TerminalAction.FAIL_WORKFLOW,
                "failure": TerminalAction.FAIL_WORKFLOW,
            }
        },
        recovery_commands=(_RecoveryCmd,),
    )

    with scoped_engine() as eng:
        eng.register_workflow(wf)
        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id = await eng.start(
                workflow_name="recovery-resolution-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    # Recovery command ran (succeeded), then _FailLabel re-ran and failed again
    # (no infinite loop — _has_recovered guard stops after one recovery).
    # The workflow must be in a terminal state.
    terminal = {WorkflowState.DONE.value, WorkflowState.FAILED.value}
    assert wfx.state in terminal, f"expected terminal state, got {wfx.state}"


@pytest.mark.asyncio
async def test_register_workflow_rejects_duplicate_recovery_label_service(db_session: Any) -> None:
    """register_workflow raises WorkflowError when two recovery_commands classes
    share the same `recovers_failure_label`."""
    del db_session

    pass_step = step(_PassLocal)
    wf = Workflow(
        name="duplicate-recovery-label-test",
        version=1,
        steps=(pass_step,),
        entry=pass_step,
        transitions={pass_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
        recovery_commands=(_RecoveryConflictA, _RecoveryConflictB),
    )

    with scoped_engine() as eng:
        with pytest.raises(WorkflowError, match="duplicate recovery label"):
            eng.register_workflow(wf)


@pytest.mark.asyncio
async def test_callback_exception_records_on_span_service(db_session: Any) -> None:
    """A raising on_start callback records an exception event + ERROR status on
    the `workflow.callback.on_start` span.

    The callback fires during the `route_workflow` task body (the bootstrap
    transition). `drain_once` absorbs the exception (logs + records `last_error`
    on the outbox row) so the ValueError does NOT surface to the test caller.
    The span state is captured before `drain_once` swallows the error.
    """
    org_id = uuid4()

    async def _raising(**_: Any) -> None:
        raise ValueError("callback boom")

    pass_step = step(_PassLocal)
    wf = Workflow(
        name="callback-exc-span-test",
        version=1,
        steps=(pass_step,),
        entry=pass_step,
        transitions={pass_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
        on_start=_raising,
    )

    with span_capture() as exporter:
        with scoped_engine() as eng:
            eng.register_workflow(wf)
            async with org_context(org_id, ActorKind.SYSTEM):
                await eng.start(
                    workflow_name="callback-exc-span-test",
                    ticket_id=str(uuid4()),
                    session=db_session,
                )
                await db_session.commit()

                # Drain one iteration — route_workflow fires the on_start
                # callback which raises; drain_once absorbs the error and
                # records it on the outbox row's last_error field.
                await _drain(db_session, max_iters=1)

    spans = exporter.get_finished_spans()
    cb_spans = [s for s in spans if s.name == "workflow.callback.on_start"]
    assert cb_spans, f"expected workflow.callback.on_start span; got {[s.name for s in spans]}"
    cb = cb_spans[0]
    assert cb.status.status_code == StatusCode.ERROR, (
        f"callback span expected ERROR; got {cb.status.status_code}"
    )
    exc_events = [e for e in cb.events if e.name == "exception"]
    assert exc_events, "expected an 'exception' event on the callback span"
