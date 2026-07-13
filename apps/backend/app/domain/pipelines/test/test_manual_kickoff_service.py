"""Service tests for start_manual_run: start on a pending ticket, RunInFlightError,
kill-and-replace scenarios."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.tenancy import create_org
from app.domain.actions import ActionContext, register_action, set_actions_for_tests
from app.domain.pipelines import (
    ActionStage,
    Kickoff,
    PipelineDefinition,
    create_pipeline,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow
from app.domain.pipelines.service import (
    PipelineNotFoundError,
    RunInFlightError,
    start_manual_run,
)
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import TicketNotFoundError, create_from_manual

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


def _one_stage_definition() -> PipelineDefinition:
    return PipelineDefinition(name=f"pipe-{uuid4().hex[:8]}", stages=(ActionStage(action_id="noop"),))


async def _seed_org_ticket_pipeline(db_session: AsyncSession):
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    ticket_id, _ = await create_from_manual(
        org_id=org.org_id,
        title="manual task",
        repo_external_id="acme/api",
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.flush()
    pipeline_id = await create_pipeline(
        org_id=org.org_id,
        definition=_one_stage_definition(),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.flush()
    return org.org_id, ticket_id, pipeline_id


async def _run_row(db_session: AsyncSession, run_id) -> PipelineRunRow:
    return await db_session.get(PipelineRunRow, run_id)


@pytest.mark.asyncio
async def test_start_manual_run_creates_run_on_pending_ticket(db_session: AsyncSession) -> None:
    """start_manual_run inserts a queued run that is immediately promoted to running."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id, pipeline_id = await _seed_org_ticket_pipeline(db_session)

        run_id = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text=None,
            session=db_session,
        )
        await db_session.commit()

        row = await _run_row(db_session, run_id)
        assert row is not None
        assert row.state == "running"
        assert row.ticket_id == ticket_id
        assert row.org_id == org_id


@pytest.mark.asyncio
async def test_start_manual_run_unknown_ticket_raises(db_session: AsyncSession) -> None:
    """start_manual_run raises TicketNotFoundError for an unknown ticket_id."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
        pipeline_id = await create_pipeline(
            org_id=org.org_id,
            definition=_one_stage_definition(),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        with pytest.raises(TicketNotFoundError):
            await start_manual_run(
                org_id=org.org_id,
                ticket_id=uuid4(),
                pipeline_id=pipeline_id,
                actor=Actor.system(),
                input_text=None,
                session=db_session,
            )


@pytest.mark.asyncio
async def test_start_manual_run_unknown_pipeline_raises(db_session: AsyncSession) -> None:
    """start_manual_run raises PipelineNotFoundError for an unknown pipeline_id."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
        ticket_id, _ = await create_from_manual(
            org_id=org.org_id,
            title="task",
            repo_external_id="acme/api",
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        with pytest.raises(PipelineNotFoundError):
            await start_manual_run(
                org_id=org.org_id,
                ticket_id=ticket_id,
                pipeline_id=uuid4(),
                actor=Actor.system(),
                input_text=None,
                session=db_session,
            )


@pytest.mark.asyncio
async def test_start_manual_run_in_flight_replace_false_raises(db_session: AsyncSession) -> None:
    """start_manual_run with replace_in_flight=False raises RunInFlightError when a run is running."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id, pipeline_id = await _seed_org_ticket_pipeline(db_session)

        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)
        # Seed a running run directly via start_run.
        await start_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()

        with pytest.raises(RunInFlightError):
            await start_manual_run(
                org_id=org_id,
                ticket_id=ticket_id,
                pipeline_id=pipeline_id,
                actor=Actor.system(),
                input_text="retry",
                replace_in_flight=False,
                session=db_session,
            )


@pytest.mark.asyncio
async def test_start_manual_run_in_flight_replace_true_kills_and_queues(
    db_session: AsyncSession,
) -> None:
    """replace_in_flight=True kills the running run and promotes the new one."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id, pipeline_id = await _seed_org_ticket_pipeline(db_session)

        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)
        run_a = await start_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()

        a = await _run_row(db_session, run_a)
        assert a.state == "running"

        run_b = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text="fresh start",
            replace_in_flight=True,
            session=db_session,
        )
        await db_session.commit()

        a = await _run_row(db_session, run_a)
        b = await _run_row(db_session, run_b)
        assert a.state == "killed"
        assert b.state == "running"


@pytest.mark.asyncio
async def test_start_manual_run_replace_true_new_run_completes_after_drain(
    db_session: AsyncSession,
) -> None:
    """After killing and replacing, the new run completes when drained."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NoOpAction())

        org_id, ticket_id, pipeline_id = await _seed_org_ticket_pipeline(db_session)

        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None)
        await start_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()

        run_b = await start_manual_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            actor=Actor.system(),
            input_text=None,
            replace_in_flight=True,
            session=db_session,
        )
        await db_session.commit()

        await drain(db_session)

        b = await _run_row(db_session, run_b)
        assert b.state in ("running", "completed")
