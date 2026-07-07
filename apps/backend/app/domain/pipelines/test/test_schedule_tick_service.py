"""Service test: `pipeline_schedule_tick` — a due schedule-kind trigger
binding fires exactly once per `fire_time`, titles the ticket from the
schedule, and notifies the schedule's `notify_user_ids` as the run's
escalation target when the run fails.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.core.audit_log import Actor
from app.core.auth import Role
from app.core.identity import create_user
from app.core.notifications import list_for_user
from app.core.tenancy import create_membership, create_org
from app.domain.actions import ActionContext, ActionError, register_action, set_actions_for_tests
from app.domain.pipelines import ActionStage, PipelineDefinition, create_pipeline
from app.domain.pipelines.scheduler_jobs import pipeline_schedule_tick
from app.domain.pipelines.test.drain import drain
from app.domain.repos import Schedule, TriggerBindingSpec, add_binding
from app.domain.tickets import TicketFilter, list_tickets

pytestmark = pytest.mark.service


class _NoteResult(BaseModel):
    note: str = "done"


class _FailingAction:
    action_id = "schedule-tick-fail-action"
    plugin_id: str | None = None
    label = "Failing test action"
    Result: ClassVar[type[BaseModel]] = _NoteResult

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        raise ActionError("boom")


async def _seed_user(db_session, *, org_id: UUID) -> UUID:
    user = await create_user(db_session, display_name="Notify Me")
    await create_membership(db_session, user_id=user.id, org_id=org_id, role=Role.BUILDER, handle="notify")
    return user.id


async def _seed_binding(
    db_session,
    *,
    org_id: UUID,
    repo_external_id: str,
    action_id: str,
    cron: str,
    notify_user_ids: tuple[UUID, ...],
) -> UUID:
    pipeline_id = await create_pipeline(
        org_id=org_id,
        definition=PipelineDefinition(
            name=f"pipe-{uuid4().hex[:8]}", stages=(ActionStage(action_id=action_id),)
        ),
        actor=Actor.system(),
        session=db_session,
    )
    return await add_binding(
        org_id,
        repo_external_id,
        spec=TriggerBindingSpec(
            intake_point_id="schedule",
            pipeline_id=pipeline_id,
            schedule=Schedule(
                name="Nightly sweep",
                cron=cron,
                notify_user_ids=notify_user_ids,
                kickoff_input="run the nightly sweep",
            ),
        ),
        actor=Actor.system(),
        session=db_session,
    )


@pytest.mark.asyncio
async def test_due_binding_fires_ticket_and_run_once_service(db_session, redis_or_skip) -> None:
    """Acceptance: a due binding creates a ticket titled from the schedule
    (with the notify list as escalation target) exactly once for a repeated
    `fire_time` — a second tick invocation with the SAME `now` is a no-op."""
    org = await create_org(db_session, slug=f"sched-tick-{uuid4().hex[:8]}", display_name="Sched Tick Org")
    user_id = await _seed_user(db_session, org_id=org.org_id)

    with set_actions_for_tests(scenario="empty"):
        register_action(_FailingAction())
        binding_id = await _seed_binding(
            db_session,
            org_id=org.org_id,
            repo_external_id="acme/scheduled-repo",
            action_id=_FailingAction.action_id,
            cron="* * * * *",
            notify_user_ids=(user_id,),
        )
        await db_session.commit()

        fixed_now = datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)

        await pipeline_schedule_tick(now=fixed_now)
        await drain(db_session)

        # Redelivery of the SAME slot must not create a second ticket/run.
        await pipeline_schedule_tick(now=fixed_now)
        await drain(db_session)

    tickets = await list_tickets(TicketFilter(repo_external_ids=["acme/scheduled-repo"]), org_id=org.org_id)
    assert len(tickets) == 1, "redelivered fire_time must not create a second ticket"
    ticket = tickets[0]
    assert ticket.title == "Nightly sweep"
    assert ticket.source_external_id == f"{binding_id}:{fixed_now.isoformat()}"
    assert ticket.branch_name is not None and ticket.branch_name.startswith("yaaos/")
    assert ticket.status == "failed"

    notifications = await list_for_user(db_session, user_id=user_id, org_id=org.org_id)
    assert any(n.subject_type == "pipeline_run" for n in notifications), (
        "the schedule's notify_user_ids must be the run's escalation target on failure"
    )


@pytest.mark.asyncio
async def test_non_matching_cron_does_not_fire_service(db_session, redis_or_skip) -> None:
    """A binding whose cron doesn't match `now` never fires."""
    org = await create_org(db_session, slug=f"sched-tick-{uuid4().hex[:8]}", display_name="No Fire Org")
    user_id = await _seed_user(db_session, org_id=org.org_id)
    await _seed_binding(
        db_session,
        org_id=org.org_id,
        repo_external_id="acme/never-fires",
        action_id="noop",
        cron="0 0 1 1 *",  # 00:00 on Jan 1st only
        notify_user_ids=(user_id,),
    )
    await db_session.commit()

    fixed_now = datetime(2027, 6, 15, 12, 30, 0, tzinfo=UTC)
    await pipeline_schedule_tick(now=fixed_now)

    tickets = await list_tickets(TicketFilter(repo_external_ids=["acme/never-fires"]), org_id=org.org_id)
    assert tickets == []
