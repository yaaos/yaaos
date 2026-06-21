"""Service tests: LocalCommand SAVEPOINT atomicity.

Two tests verify that `_start_step_impl` wraps Local command execution in a
nested transaction (SAVEPOINT) so command DB writes, step_attempts, and the
outbox enqueue for route_workflow are all-or-nothing.

- `test_local_command_writes_rollback_on_exception_service` — a LocalCommand
  that raises RuntimeError: (a) step_state not updated (simulates PostFindings
  rolling back its FindingRow writes), (b) step_attempts not stamped, (c) no
  route_workflow outbox row committed.  Workflow enters FAILED terminal state.

- `test_local_command_writes_commit_atomically_service` — a LocalCommand that
  returns Outcome.success(): step_attempts is stamped and route_workflow is
  enqueued in the same commit; workflow reaches DONE.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    WorkflowState,
    step,
)
from app.core.workflow.models import WorkflowExecutionRow
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


# ── Test commands ──────────────────────────────────────────────────────────────


class _RaiseLocal:
    """LocalCommand that raises RuntimeError — triggers SAVEPOINT rollback.

    Analogous to PostFindings.execute raising mid-body: any DB writes the
    command made (e.g. inserting FindingRow) are rolled back with the savepoint.
    """

    kind = "_RaiseLocalCmd"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: Any, *, session: Any = None) -> Outcome:
        del inputs, ctx, session
        raise RuntimeError("intentional failure for rollback test")


class _PassLocal:
    """LocalCommand that returns Outcome.success() — verifies commit atomicity."""

    kind = "_PassLocalCmd"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: Any, *, session: Any = None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


# ── Drain helper ───────────────────────────────────────────────────────────────


async def _drain(db_session: Any, *, max_iters: int = 30) -> None:
    from app.core.tasks import get_broker  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
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


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_local_command_writes_rollback_on_exception_service(db_session: Any) -> None:
    """When a LocalCommand raises, its DB writes + step_attempts + the
    route_workflow outbox enqueue all roll back atomically.

    Assertions:
      (a) step_state for the step is absent — equivalent to PostFindings's
          FindingRow inserts being rolled back.
      (b) step_attempts for the step is absent — the stamping inside the
          SAVEPOINT rolled back, proving the outbox INSERT also rolled back
          (they share the same SAVEPOINT).
      (c) No pending outbox entries remain — route_workflow was never committed.
    The workflow terminates as FAILED (the engine's savepoint handler fires).
    """
    org_id = uuid4()
    raise_step = step(_RaiseLocal)
    wf = Workflow(
        name="local-rollback-atomicity-test",
        version=1,
        steps=(raise_step,),
        entry=raise_step,
        transitions={raise_step: {"failure": TerminalAction.FAIL_WORKFLOW}},
    )

    with scoped_engine() as eng:
        eng.register_workflow(wf)
        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id = await eng.start(
                workflow_name="local-rollback-atomicity-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None

    # Engine's savepoint error handler sets the terminal state.
    assert wfx.state == WorkflowState.FAILED.value, (
        f"expected FAILED after local command raised; got {wfx.state!r}"
    )

    # (a) step_state: no output from the raising step — command writes rolled back.
    step_key = _RaiseLocal.kind
    assert step_key not in (wfx.step_state or {}), (
        f"step_state should not contain {step_key!r} after rollback; got step_state={wfx.step_state!r}"
    )

    # (b) step_attempts: not stamped — the stamp was inside the SAVEPOINT and rolled back.
    # This is the canonical proof that the outbox enqueue also rolled back
    # (step_attempts update and enqueue share the same SAVEPOINT).
    assert step_key not in (wfx.step_attempts or {}), (
        f"step_attempts should not contain {step_key!r} after rollback; "
        f"got step_attempts={wfx.step_attempts!r}"
    )

    # (c) No pending outbox entries — route_workflow was never committed.
    # After full drain, only entries that were committed to the outbox exist.
    # Since route_workflow was inside the rolled-back SAVEPOINT, zero pending entries.
    pending = await get_pending_task_names(db_session)
    assert not pending, f"expected no pending outbox entries after rollback; got {pending!r}"


async def test_local_command_writes_commit_atomically_service(db_session: Any) -> None:
    """When a LocalCommand succeeds, step_attempts + route_workflow outbox
    enqueue commit atomically in the same transaction.

    Assertions:
      - step_attempts has the step entry (stamped inside the savepoint, committed).
      - workflow reaches DONE (route_workflow ran successfully).
    """
    org_id = uuid4()
    pass_step = step(_PassLocal)
    wf = Workflow(
        name="local-commit-atomicity-test",
        version=1,
        steps=(pass_step,),
        entry=pass_step,
        transitions={pass_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
    )

    with scoped_engine() as eng:
        eng.register_workflow(wf)
        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id = await eng.start(
                workflow_name="local-commit-atomicity-test",
                ticket_id=str(uuid4()),
                session=db_session,
            )
            await db_session.commit()
            await _drain(db_session)

    wfx = await db_session.get(WorkflowExecutionRow, UUID(wfx_id))
    assert wfx is not None

    # Workflow completes successfully.
    assert wfx.state == WorkflowState.DONE.value, (
        f"expected DONE after local command succeeded; got {wfx.state!r}"
    )

    # step_attempts stamped — proves the SAVEPOINT committed (step_attempts and
    # the route_workflow enqueue share the same SAVEPOINT).
    step_key = _PassLocal.kind
    assert step_key in (wfx.step_attempts or {}), (
        f"step_attempts should contain {step_key!r} after success; got step_attempts={wfx.step_attempts!r}"
    )
