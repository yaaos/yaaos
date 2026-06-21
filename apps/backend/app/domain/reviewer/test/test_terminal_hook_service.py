"""Service test: reviewer workflow on_terminal callback flips the owning ticket atomically.

The callback is attached directly to `Workflow.on_terminal` — no separate
registration call needed. Tests verify:

  1. DONE  → ticket status becomes "done"
  2. FAILED → ticket status becomes "failed"; failure_reason threads into the audit
  3. CANCELLED → ticket status becomes "cancelled"
  4. Non-owning execution → ticket untouched (different current_workflow_execution_id)
  5. workflow with no on_terminal → ticket untouched
  6. Redelivered terminal (ticket already terminal) → no-op, no raise

Does NOT invoke the orphan sweep — the atomic callback is the tested path here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from app.core.audit_log import list_for_entity
from app.core.tasks import drain_once, get_broker, get_pending_task_names
from app.core.workflow import (
    CommandContext,
    Empty,
    Outcome,
    TerminalAction,
    Workflow,
    step,
)
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import get as get_ticket
from app.domain.tickets import set_workflow_execution, transition_ticket_on_terminal
from app.testing.workflow_harness import set_engine_for_tests

pytestmark = pytest.mark.service

# ── Stub commands ─────────────────────────────────────────────────────────────


class _LocalSuccess:
    """A LOCAL command that always returns success."""

    kind = "TermHookSuccessCmd"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.success()


class _LocalFail:
    """A LOCAL command that always returns failure (default reason)."""

    kind = "TermHookFailCmd"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.failure(reason="test_failure")


class _LocalFailWithReason:
    """A LOCAL command that fails with a specific reason threaded into the audit."""

    kind = "TermHookFailWithReasonCmd"
    Inputs = Empty
    Outputs = Empty

    async def execute(self, inputs: Empty, ctx: CommandContext, *, session=None) -> Outcome:
        del inputs, ctx, session
        return Outcome.failure(reason="schema_invalid")


_success_step = step(_LocalSuccess)
_fail_step = step(_LocalFail)
_fail_reason_step = step(_LocalFailWithReason)


# ── Minimal workflow_input with org_id so the callback can find it ────────────


class _OrgInput(BaseModel):
    model_config = ConfigDict(frozen=True)
    org_id: UUID


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _drain(db_session: object, *, max_iterations: int = 50) -> int:
    """Drain the taskiq outbox until empty, dispatching each task body inline."""

    async def _dispatch(kind: str, payload: dict) -> None:  # type: ignore[type-arg]
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    total = 0
    for _ in range(max_iterations):
        pending = await get_pending_task_names(db_session)  # type: ignore[arg-type]
        if not pending:
            return total
        delivered = await drain_once(db_session, dispatcher=_dispatch)  # type: ignore[arg-type]
        await db_session.commit()  # type: ignore[union-attr]
        total += delivered
        if delivered == 0:
            break
    return total


async def _seed_running_ticket(db_session: object, *, org_id: UUID) -> UUID:
    """Create a minimal ticket in `running` status; return its id."""
    ext_id = f"hook-test-{uuid4().hex[:8]}"
    ticket_id, _ = await create_ticket(
        org_id=org_id,
        source_external_id=ext_id,
        title=f"hook-test {ext_id}",
        description=None,
        repo_external_id="me/repo",
        plugin_id="github",
        idempotency_key=ext_id,
        payload={
            "is_draft": False,
            "is_fork": False,
            "labels": [],
            "author_login": "alice",
            "head_sha": "abc",
            "base_sha": "def",
        },
        session=db_session,  # type: ignore[arg-type]
    )
    return ticket_id


def _pr_review_wf(*steps, transitions) -> Workflow:  # type: ignore[no-untyped-def]
    """Build a minimal pr_review_v1 workflow with on_terminal attached."""
    entry = steps[0]
    return Workflow(
        name="pr_review_v1",
        version=1,
        steps=steps,
        entry=entry,
        transitions=transitions,
        on_terminal=transition_ticket_on_terminal,
    )


# ── Scenarios ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_done_workflow_flips_ticket_to_done(db_session) -> None:
    """DONE workflow → owning ticket becomes "done" in the same transaction."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with set_engine_for_tests() as eng:
        eng.register_workflow(
            _pr_review_wf(
                _success_step,
                transitions={_success_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
            )
        )

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id_str = await eng.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                workflow_input=_OrgInput(org_id=org_id),
                session=db_session,
            )
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=UUID(wfx_id_str),
                session=db_session,
            )
            await db_session.commit()

            await _drain(db_session)

    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status == "done"


@pytest.mark.asyncio
async def test_failed_workflow_flips_ticket_to_failed_with_reason(db_session) -> None:
    """FAILED workflow → ticket becomes "failed"; failure_reason threads into audit."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with set_engine_for_tests() as eng:
        eng.register_workflow(
            _pr_review_wf(
                _fail_reason_step,
                transitions={_fail_reason_step: {"failure": TerminalAction.FAIL_WORKFLOW}},
            )
        )

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id_str = await eng.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                workflow_input=_OrgInput(org_id=org_id),
                session=db_session,
            )
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=UUID(wfx_id_str),
                session=db_session,
            )
            await db_session.commit()

            await _drain(db_session)

    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status == "failed"

    # Audit row carries the failure reason in its payload.
    audits = await list_for_entity("ticket", ticket_id, org_id=org_id, kinds=["ticket.status_changed"])
    assert len(audits) >= 1
    terminal_audit = audits[-1]
    assert terminal_audit.payload.get("to_status") == "failed"
    assert terminal_audit.payload.get("reason") == "schema_invalid"


@pytest.mark.asyncio
async def test_cancelled_workflow_flips_ticket_to_cancelled(db_session) -> None:
    """CANCELLED workflow (cancel_requested=True) → ticket becomes "cancelled"."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.workflow import request_cancel  # noqa: PLC0415

    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with set_engine_for_tests() as eng:
        eng.register_workflow(
            _pr_review_wf(
                _success_step,
                transitions={_success_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
            )
        )

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id_str = await eng.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                workflow_input=_OrgInput(org_id=org_id),
                session=db_session,
            )
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=UUID(wfx_id_str),
                session=db_session,
            )
            await db_session.commit()

            # Mark cancel_requested before the engine drains the outbox so
            # route_workflow picks it up and routes to CANCELLED.
            await request_cancel(wfx_id_str, session=db_session)
            await db_session.commit()

            await _drain(db_session)

    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status == "cancelled"


@pytest.mark.asyncio
async def test_non_owning_execution_leaves_ticket_untouched(db_session) -> None:
    """A terminal event from a superseded execution must not flip the ticket.

    The ticket's current_workflow_execution_id is deliberately set to a
    different UUID than the one reaching terminal state, simulating a newer
    execution that has taken ownership.
    """
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)
    # Point the ticket at a DIFFERENT (phantom) execution id.
    other_wfx_id = uuid4()

    with set_engine_for_tests() as eng:
        eng.register_workflow(
            _pr_review_wf(
                _success_step,
                transitions={_success_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
            )
        )

        async with org_context(org_id, ActorKind.SYSTEM):
            await eng.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                workflow_input=_OrgInput(org_id=org_id),
                session=db_session,
            )
            # Set the ticket to point at a *different* execution — not the one
            # we just started.
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=other_wfx_id,
                session=db_session,
            )
            await db_session.commit()

            await _drain(db_session)

    # Ticket still non-terminal ("pending") — the non-owning execution's callback was a no-op.
    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status not in ("done", "failed", "cancelled")


@pytest.mark.asyncio
async def test_no_on_terminal_leaves_ticket_untouched(db_session) -> None:
    """A workflow with no on_terminal callback must not flip the ticket.

    A workflow without on_terminal (default None) running to completion
    leaves the ticket in whatever status it was in before.
    """
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with set_engine_for_tests() as eng:
        eng.register_workflow(
            Workflow(
                name="other_workflow_v1",
                version=1,
                steps=(_success_step,),
                entry=_success_step,
                transitions={_success_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
                # no on_terminal
            )
        )

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id_str = await eng.start(
                workflow_name="other_workflow_v1",
                ticket_id=str(ticket_id),
                workflow_input=_OrgInput(org_id=org_id),
                session=db_session,
            )
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=UUID(wfx_id_str),
                session=db_session,
            )
            await db_session.commit()

            await _drain(db_session)

    # Ticket still non-terminal — no on_terminal, no flip.
    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status not in ("done", "failed", "cancelled")


@pytest.mark.asyncio
async def test_redelivered_terminal_is_noop_no_raise(db_session) -> None:
    """A terminal callback called on an already-terminal ticket must not raise.

    Simulates a redelivery scenario: ticket is already "done" before the
    engine fires (or fires a second time). The callback must return without
    raising, preserving the engine's transaction.
    """
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with set_engine_for_tests() as eng:
        eng.register_workflow(
            _pr_review_wf(
                _success_step,
                transitions={_success_step: {"success": TerminalAction.COMPLETE_WORKFLOW}},
            )
        )

        async with org_context(org_id, ActorKind.SYSTEM):
            wfx_id_str = await eng.start(
                workflow_name="pr_review_v1",
                ticket_id=str(ticket_id),
                workflow_input=_OrgInput(org_id=org_id),
                session=db_session,
            )
            await set_workflow_execution(
                ticket_id,
                workflow_execution_id=UUID(wfx_id_str),
                session=db_session,
            )
            await db_session.commit()

            # Drive the workflow to terminal once — ticket flips to "done".
            await _drain(db_session)

            ticket_after_first = await get_ticket(ticket_id, org_id=org_id)
            assert ticket_after_first.status == "done"

            # Second drain: workflow is already terminal; route_workflow exits
            # early (skip_terminal). No new outbox rows, no raise.
            await _drain(db_session)

    # Still "done" — no exception, no double-write.
    ticket_after_second = await get_ticket(ticket_id, org_id=org_id)
    assert ticket_after_second.status == "done"
    audits = await list_for_entity("ticket", ticket_id, org_id=org_id, kinds=["ticket.status_changed"])
    # Only one transition written, not two.
    terminal_audits = [a for a in audits if a.payload.get("to_status") in ("done", "failed", "cancelled")]
    assert len(terminal_audits) == 1
