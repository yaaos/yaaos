"""Service test: one-in-flight-per-ticket queueing — queued-if-busy,
promotion at terminal, the promotion-race guard (`ux_pipeline_runs_one_in_flight`)
never leaves two runs `running` on one ticket, and cancel of both a
`running` and a `queued` run.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from app.core.audit_log import Actor, ActorKind, list_for_entity
from app.core.auth import org_context
from app.core.tenancy import create_org
from app.domain.actions import ActionContext, register_action, set_actions_for_tests
from app.domain.pipelines import (
    ActionStage,
    Kickoff,
    PipelineDefinition,
    create_pipeline,
    has_run_in_flight,
    request_cancel,
    start_run,
)
from app.domain.pipelines import engine as pipelines_engine
from app.domain.pipelines.models import PipelineRunRow
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.service


class _NoteResult(BaseModel):
    note: str = "done"


class _NoOpAction:
    action_id = "noop"
    plugin_id: str | None = None
    label = "No-op test action"
    Result: ClassVar[type[BaseModel]] = _NoteResult

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        return _NoteResult()


async def _seed_org_and_ticket(db_session):
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="queueing test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.execute(
        text("UPDATE tickets SET branch_name = :branch WHERE id = :id"),
        {"branch": "yaaos/test-branch", "id": ticket_id},
    )
    await db_session.flush()
    return org.org_id, ticket_id


async def _run_row(db_session, run_id) -> PipelineRunRow:
    return await db_session.get(PipelineRunRow, run_id)


def _one_stage_definition() -> PipelineDefinition:
    return PipelineDefinition(name=f"pipe-{uuid4().hex[:8]}", stages=(ActionStage(action_id="noop"),))


@pytest.mark.asyncio
async def test_second_run_queues_until_first_terminal_promotes_it_service(db_session) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_one_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)

        run_a = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        a = await _run_row(db_session, run_a)
        assert a.state == "running"

        run_b = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        b = await _run_row(db_session, run_b)
        assert b.state == "queued"

        # Drain run A to its terminal — its own terminal handling promotes
        # the oldest queued run (B).
        await drain(db_session)

        a = await _run_row(db_session, run_a)
        assert a.state == "completed"
        b = await _run_row(db_session, run_b)
        assert b.state in ("running", "completed")


@pytest.mark.asyncio
async def test_promotion_race_never_leaves_two_runs_running_service(db_session) -> None:
    """Three runs on one ticket: A running, B + C queued. A concurrent
    promotion attempt on the already-running slot is rejected by the unique
    index; the terminal-triggered sweep promotes only the oldest queued run."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_one_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)

        run_a = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        run_b = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        run_c = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()

        a = await _run_row(db_session, run_a)
        b = await _run_row(db_session, run_b)
        c = await _run_row(db_session, run_c)
        assert a.state == "running"
        assert b.state == "queued"
        assert c.state == "queued"

        # A second promotion attempt on B while A still holds the slot is
        # rejected — the unique index, not luck, is what guards this.
        promoted = await pipelines_engine.attempt_promotion(b, session=db_session)
        assert promoted is False
        b = await _run_row(db_session, run_b)
        assert b.state == "queued"

        # Simulate A reaching terminal directly (bypassing the taskiq trio)
        # to isolate the promotion-sweep behavior from stage execution.
        await db_session.execute(
            text("UPDATE pipeline_runs SET state = 'completed' WHERE id = :id"), {"id": run_a}
        )
        await pipelines_engine.promote_oldest_queued(ticket_id, session=db_session)
        await db_session.commit()

        b = await _run_row(db_session, run_b)
        c = await _run_row(db_session, run_c)
        # B is the oldest queued run (uuid7 order) — it is promoted; C stays queued.
        assert b.state == "running"
        assert c.state == "queued"

        # With B now holding the slot, promoting C is rejected the same way.
        promoted_c = await pipelines_engine.attempt_promotion(c, session=db_session)
        assert promoted_c is False


@pytest.mark.asyncio
async def test_cancel_running_run_defers_to_next_boundary_service(db_session) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_one_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)

        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        run = await _run_row(db_session, run_id)
        assert run.state == "running"

        async with org_context(org_id, ActorKind.SYSTEM):
            await request_cancel(run_id, actor=Actor.system(), session=db_session)
        await db_session.commit()

        run = await _run_row(db_session, run_id)
        assert run.state == "running"
        assert run.cancel_requested is True

        await drain(db_session)

        run = await _run_row(db_session, run_id)
        assert run.state == "cancelled"

        entries = await list_for_entity("pipeline_run", run_id, org_id=org_id)
        assert "run.cancelled" in [e.kind for e in entries]


@pytest.mark.asyncio
async def test_cancel_queued_run_is_immediate_service(db_session) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_one_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)

        run_a = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        run_b = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        b = await _run_row(db_session, run_b)
        assert b.state == "queued"

        async with org_context(org_id, ActorKind.SYSTEM):
            await request_cancel(run_b, actor=Actor.system(), session=db_session)
        await db_session.commit()

        b = await _run_row(db_session, run_b)
        assert b.state == "cancelled"
        a = await _run_row(db_session, run_a)
        assert a.state == "running"

        entries = await list_for_entity("pipeline_run", run_b, org_id=org_id)
        assert "run.cancelled" in [e.kind for e in entries]


@pytest.mark.asyncio
async def test_has_run_in_flight_returns_true_for_running_run_service(db_session) -> None:
    """A running run makes `has_run_in_flight` return True."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_one_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)

        # No runs yet — not in flight.
        assert await has_run_in_flight(ticket_id, session=db_session) is False

        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        run = await _run_row(db_session, run_id)
        assert run.state == "running"

        assert await has_run_in_flight(ticket_id, session=db_session) is True


@pytest.mark.asyncio
async def test_has_run_in_flight_includes_queued_run_service(db_session) -> None:
    """A queued run (waiting for the one-in-flight slot) also counts as in flight —
    the ticket has committed pipeline work and starting another run would be redundant."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_one_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)

        # Start A (promoted to running) then B (left queued).
        await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        run_b = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        b = await _run_row(db_session, run_b)
        assert b.state == "queued"

        # Both running and queued count as in-flight.
        assert await has_run_in_flight(ticket_id, session=db_session) is True


@pytest.mark.asyncio
async def test_has_run_in_flight_returns_false_after_terminal_service(db_session) -> None:
    """Once the run completes (and no queued runs remain), the gate returns False."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())
        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_one_stage_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)

        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        assert await has_run_in_flight(ticket_id, session=db_session) is True

        await drain(db_session)

        run = await _run_row(db_session, run_id)
        assert run.state == "completed"
        assert await has_run_in_flight(ticket_id, session=db_session) is False
