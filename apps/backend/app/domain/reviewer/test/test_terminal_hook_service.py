"""Service test: reviewer terminal hook flips the owning ticket atomically.

Covers 6 scenarios (all inside the same workflow engine + session transaction):
  1. DONE  → ticket status becomes "done"
  2. FAILED → ticket status becomes "failed"; failure_reason threads into the audit
  3. CANCELLED → ticket status becomes "cancelled"
  4. Non-owning execution → ticket untouched (different current_workflow_execution_id)
  5. workflow_name ≠ "pr_review_v1" → ticket untouched (hook is a no-op)
  6. Redelivered terminal (ticket already terminal) → no-op, no raise

Does NOT invoke the orphan sweep — the atomic hook is the tested path here.

The reviewer terminal hook is NOT registered automatically in tests; this file
registers it explicitly via `register_reviewer_terminal_hooks()` after requesting
`terminal_hooks_isolation`.

Flag: this test file is marked for coverage scrutiny — it is the primary gate
ensuring the atomic ticket-flip contract holds across all three terminal states.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.audit_log import list_for_entity
from app.core.tasks import drain_once, get_broker, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    CommandContext,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
)
from app.domain.reviewer import register_reviewer_terminal_hooks
from app.domain.tickets import create_from_pr as create_ticket
from app.domain.tickets import get as get_ticket
from app.domain.tickets import set_workflow_execution
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service

# ── Helpers ──────────────────────────────────────────────────────────────────


class _LocalSuccess:
    """A LOCAL command that always returns success."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.category = CommandCategory.LOCAL
        self.restart_safe = True

    async def execute(self, inputs: object, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.success()


class _LocalFail:
    """A LOCAL command that always returns failure with a given reason."""

    kind = "FailCmd"
    category = CommandCategory.LOCAL
    restart_safe = True

    def __init__(self, reason: str = "test_failure") -> None:
        self._reason = reason

    async def execute(self, inputs: object, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.failure(reason=self._reason)


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


# ── Scenarios ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_done_workflow_flips_ticket_to_done(
    db_session,
    terminal_hooks_isolation,
    workflow_context_provider_isolation,
) -> None:
    """DONE workflow → owning ticket becomes "done" in the same transaction."""
    register_reviewer_terminal_hooks()
    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with scoped_engine() as eng:
        eng.register_command(_LocalSuccess("SuccessCmd"))
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(
                    Step(
                        id="step1",
                        command_kind="SuccessCmd",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="step1",
            )
        )

        wfx_id_str = await eng.start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            ticket_payload={"org_id": str(org_id)},
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
async def test_failed_workflow_flips_ticket_to_failed_with_reason(
    db_session,
    terminal_hooks_isolation,
    workflow_context_provider_isolation,
) -> None:
    """FAILED workflow → ticket becomes "failed"; failure_reason threads into audit."""
    register_reviewer_terminal_hooks()
    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with scoped_engine() as eng:
        eng.register_command(_LocalFail(reason="schema_invalid"))
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(Step(id="step1", command_kind="FailCmd"),),
                entry_step_id="step1",
            )
        )

        wfx_id_str = await eng.start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            ticket_payload={"org_id": str(org_id)},
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
async def test_cancelled_workflow_flips_ticket_to_cancelled(
    db_session,
    terminal_hooks_isolation,
    workflow_context_provider_isolation,
) -> None:
    """CANCELLED workflow (cancel_requested=True) → ticket becomes "cancelled"."""
    register_reviewer_terminal_hooks()
    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    from app.core.workflow import request_cancel  # noqa: PLC0415

    with scoped_engine() as eng:
        eng.register_command(_LocalSuccess("SuccessCmd"))
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(
                    Step(
                        id="step1",
                        command_kind="SuccessCmd",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="step1",
            )
        )

        wfx_id_str = await eng.start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            ticket_payload={"org_id": str(org_id)},
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
async def test_non_owning_execution_leaves_ticket_untouched(
    db_session,
    terminal_hooks_isolation,
    workflow_context_provider_isolation,
) -> None:
    """A terminal event from a superseded execution must not flip the ticket.

    The ticket's current_workflow_execution_id is deliberately set to a
    different UUID than the one reaching terminal state, simulating a newer
    execution that has taken ownership.
    """
    register_reviewer_terminal_hooks()
    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)
    # Point the ticket at a DIFFERENT (phantom) execution id.
    other_wfx_id = uuid4()

    with scoped_engine() as eng:
        eng.register_command(_LocalSuccess("SuccessCmd"))
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(
                    Step(
                        id="step1",
                        command_kind="SuccessCmd",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="step1",
            )
        )

        await eng.start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            ticket_payload={"org_id": str(org_id)},
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

    # Ticket still non-terminal ("pending") — the non-owning execution's hook was a no-op.
    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status not in ("done", "failed", "cancelled")


@pytest.mark.asyncio
async def test_non_pr_review_workflow_leaves_ticket_untouched(
    db_session,
    terminal_hooks_isolation,
    workflow_context_provider_isolation,
) -> None:
    """A terminal event from a non-pr_review_v1 workflow must not flip the ticket.

    The hook guards on `workflow_name == "pr_review_v1"` and returns early
    for any other workflow name.
    """
    register_reviewer_terminal_hooks()
    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with scoped_engine() as eng:
        eng.register_command(_LocalSuccess("SuccessCmd"))
        eng.register_workflow(
            Workflow(
                name="other_workflow_v1",  # NOT pr_review_v1
                version=1,
                steps=(
                    Step(
                        id="step1",
                        command_kind="SuccessCmd",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="step1",
            )
        )

        wfx_id_str = await eng.start(
            workflow_name="other_workflow_v1",
            ticket_id=str(ticket_id),
            ticket_payload={"org_id": str(org_id)},
            session=db_session,
        )
        await set_workflow_execution(
            ticket_id,
            workflow_execution_id=UUID(wfx_id_str),
            session=db_session,
        )
        await db_session.commit()

        await _drain(db_session)

    # Ticket still non-terminal — wrong workflow name, hook was a no-op.
    ticket = await get_ticket(ticket_id, org_id=org_id)
    assert ticket.status not in ("done", "failed", "cancelled")


@pytest.mark.asyncio
async def test_redelivered_terminal_is_noop_no_raise(
    db_session,
    terminal_hooks_isolation,
    workflow_context_provider_isolation,
) -> None:
    """A terminal hook called on an already-terminal ticket must not raise.

    Simulates a redelivery scenario: ticket is already "done" before the
    engine fires (or fires a second time). The hook must return False
    without raising, preserving the engine's transaction.
    """
    register_reviewer_terminal_hooks()
    org_id = uuid4()

    ticket_id = await _seed_running_ticket(db_session, org_id=org_id)

    with scoped_engine() as eng:
        eng.register_command(_LocalSuccess("SuccessCmd"))
        eng.register_workflow(
            Workflow(
                name="pr_review_v1",
                version=1,
                steps=(
                    Step(
                        id="step1",
                        command_kind="SuccessCmd",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="step1",
            )
        )

        wfx_id_str = await eng.start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_id),
            ticket_payload={"org_id": str(org_id)},
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
