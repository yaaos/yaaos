"""Service test: `start_rerun` — a new run on the same ticket, cloning the
prior run's `Kickoff` and starting at stage index 0 on the current
definition.

Acceptance:
- A `failed` current run reruns: a new `pipeline_runs` row is created at
  stage index 0, with a cloned `Kickoff` (`intake_point_id="rerun"`,
  `revision=None`, `input_text` preserved, `actor` replaced by the caller).
- A `completed` (non-terminal-failure) current run cannot be rerun —
  `RunNotRerunnableError`.
- A superseded run (no longer the ticket's current run) cannot be rerun —
  `RunNotRerunnableError`, even though its own state is `failed`.
- An unknown `run_id` raises `RunNotFoundError`.

Uses action-stage-only pipelines (no coding-agent/workspace stub needed) —
`ActionError` drives a run to `failed`; two succeeding actions drive a run
to `completed`. Uses the shared `drain` outbox-dispatch helper
(`test/drain.py`).
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID, uuid4, uuid7

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.identity import create_user
from app.core.tenancy import create_membership, create_org
from app.domain.actions import ActionContext, ActionError, register_action, set_actions_for_tests
from app.domain.pipelines import (
    ActionStage,
    Kickoff,
    PipelineDefinition,
    RunNotFoundError,
    RunNotRerunnableError,
    create_pipeline,
    start_rerun,
    start_run,
)
from app.domain.pipelines.models import PipelineRunRow
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.service


class _NoteResult(BaseModel):
    note: str = "done"


class _RecordingAction:
    plugin_id: str | None = None
    label = "Recording test action"
    Result: ClassVar[type[BaseModel]] = _NoteResult

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        return _NoteResult(note=self.action_id)


class _FailingAction:
    action_id = "fail-action"
    plugin_id: str | None = None
    label = "Failing test action"
    Result: ClassVar[type[BaseModel]] = _NoteResult

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        raise ActionError("boom")


async def _seed_org_ticket_and_user(db_session) -> tuple[UUID, UUID, UUID]:
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    user = await create_user(db_session, display_name="Requester")
    await create_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="requester"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="rerun test ticket",
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
    return org.org_id, ticket_id, user.id


def _failing_definition() -> PipelineDefinition:
    return PipelineDefinition(name=f"pipe-{uuid4().hex[:8]}", stages=(ActionStage(action_id="fail-action"),))


def _succeeding_definition(action_id: str) -> PipelineDefinition:
    return PipelineDefinition(name=f"pipe-{uuid4().hex[:8]}", stages=(ActionStage(action_id=action_id),))


@pytest.mark.asyncio
async def test_rerun_on_failed_run_starts_new_run_at_stage_zero_service(db_session, redis_or_skip) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_FailingAction())

        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id, definition=_failing_definition(), actor=Actor.system(), session=db_session
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user_id), input_text="build it")
        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "failed"

        rerun_actor = Actor.user(user_id=user_id)
        rerun_id = await start_rerun(org_id=org_id, run_id=run_id, actor=rerun_actor, session=db_session)
        await db_session.commit()

        assert rerun_id != run_id
        rerun = await db_session.get(PipelineRunRow, rerun_id)
        assert rerun is not None
        assert rerun.ticket_id == ticket_id
        assert rerun.pipeline_id == pipeline_id
        assert rerun.current_stage_index in (0, None)
        assert rerun.state in ("queued", "running")

        cloned_kickoff = Kickoff.model_validate(rerun.kickoff)
        assert cloned_kickoff.intake_point_id == "rerun"
        assert cloned_kickoff.revision is None
        assert cloned_kickoff.input_text == "build it"
        assert cloned_kickoff.actor.user_id == user_id


@pytest.mark.asyncio
async def test_rerun_on_completed_run_is_rejected_service(db_session, redis_or_skip) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_RecordingAction("note-a"))

        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_succeeding_definition("note-a"),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user_id), input_text=None)
        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()
        await drain(db_session)

        run = await db_session.get(PipelineRunRow, run_id)
        assert run is not None
        assert run.state == "completed"

        with pytest.raises(RunNotRerunnableError):
            await start_rerun(
                org_id=org_id, run_id=run_id, actor=Actor.user(user_id=user_id), session=db_session
            )


@pytest.mark.asyncio
async def test_rerun_on_superseded_run_is_rejected_service(db_session, redis_or_skip) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_FailingAction())
        register_action(_RecordingAction("note-b"))

        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        failing_pipeline_id = await create_pipeline(
            org_id=org_id, definition=_failing_definition(), actor=Actor.system(), session=db_session
        )
        succeeding_pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_succeeding_definition("note-b"),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user_id), input_text=None)
        first_run_id = await start_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=failing_pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        first_run = await db_session.get(PipelineRunRow, first_run_id)
        assert first_run is not None
        assert first_run.state == "failed"

        second_run_id = await start_run(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=succeeding_pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

        second_run = await db_session.get(PipelineRunRow, second_run_id)
        assert second_run is not None
        assert second_run.state == "completed"

        with pytest.raises(RunNotRerunnableError):
            await start_rerun(
                org_id=org_id, run_id=first_run_id, actor=Actor.user(user_id=user_id), session=db_session
            )


@pytest.mark.asyncio
async def test_rerun_on_unknown_run_id_raises_not_found_service(db_session) -> None:
    org_id, _, user_id = await _seed_org_ticket_and_user(db_session)
    with pytest.raises(RunNotFoundError):
        await start_rerun(
            org_id=org_id, run_id=uuid7(), actor=Actor.user(user_id=user_id), session=db_session
        )
