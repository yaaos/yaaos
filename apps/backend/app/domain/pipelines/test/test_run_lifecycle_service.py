"""Service test: a two-action-stage run executes end-to-end through the
`ROUTE_RUN`/`START_STAGE` taskiq trio (Acceptance) — plus the `ActionError`
failure path with terminal notification.

Uses the shared `drain` outbox-dispatch helper (`test/drain.py`, cloned from
`apps/backend/app/core/workflow/test/test_cancel_service.py:149`) and test
actions registered via `domain/actions.set_actions_for_tests`.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from app.core.audit_log import Actor, list_for_entity
from app.core.auth import Role
from app.core.identity import create_user
from app.core.notifications import list_for_user
from app.core.sse import subscribe_general
from app.core.tenancy import create_membership, create_org
from app.domain.actions import ActionContext, ActionError, register_action, set_actions_for_tests
from app.domain.pipelines import ActionStage, Kickoff, PipelineDefinition, create_pipeline, start_run
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.service


class _NoteResult(BaseModel):
    note: str = "done"


class _RecordingAction:
    plugin_id: str | None = None
    label = "Recording test action"
    Result: ClassVar[type[BaseModel]] = _NoteResult
    calls: ClassVar[list[str]] = []

    def __init__(self, action_id: str) -> None:
        self.action_id = action_id

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        type(self).calls.append(self.action_id)
        return _NoteResult(note=self.action_id)


class _FailingAction:
    action_id = "fail-action"
    plugin_id: str | None = None
    label = "Failing test action"
    Result: ClassVar[type[BaseModel]] = _NoteResult

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        raise ActionError("boom")


async def _seed_org_ticket_and_user(db_session):
    org = await create_org(db_session, slug=f"org-{uuid4().hex[:8]}", display_name="Test Org")
    user = await create_user(db_session, display_name="Watcher")
    await create_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="watcher"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="engine test ticket",
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


def _two_action_definition(action_ids: list[str]) -> PipelineDefinition:
    return PipelineDefinition(
        name=f"pipe-{uuid4().hex[:8]}",
        stages=tuple(ActionStage(action_id=a) for a in action_ids),
    )


@pytest.mark.asyncio
async def test_two_action_stage_run_completes_service(db_session, redis_or_skip) -> None:
    """Acceptance: a two-action-stage run completes; two stage_executions
    rows carry `action_result`; a `run.completed` audit row exists; an SSE
    `run_state_changed` publication fires."""
    _RecordingAction.calls = []
    with set_actions_for_tests(scenario="empty"):
        register_action(_RecordingAction("note-a"))
        register_action(_RecordingAction("note-b"))

        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_two_action_definition(["note-a", "note-b"]),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        received: list[dict] = []

        async def _consume() -> None:
            async for event in subscribe_general(org_id):
                if event.get("kind") == "run_state_changed":
                    received.append(event)

        consumer = asyncio.create_task(_consume())
        await asyncio.sleep(0.1)

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user_id), input_text=None)
        run_id = await start_run(
            org_id=org_id, ticket_id=ticket_id, pipeline_id=pipeline_id, kickoff=kickoff, session=db_session
        )
        await db_session.commit()

        await drain(db_session)
        await asyncio.sleep(0.3)

        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

    run = (
        await db_session.execute(text("SELECT state FROM pipeline_runs WHERE id = :id"), {"id": run_id})
    ).one()
    assert run.state == "completed"

    stage_rows = (
        (
            await db_session.execute(
                text(
                    "SELECT stage_name, status, action_result FROM stage_executions "
                    "WHERE run_id = :run_id ORDER BY stage_index"
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .all()
    )
    assert [r["stage_name"] for r in stage_rows] == ["note-a", "note-b"]
    assert all(r["status"] == "completed" for r in stage_rows)
    assert all(r["action_result"] is not None for r in stage_rows)
    assert _RecordingAction.calls == ["note-a", "note-b"]

    entries = await list_for_entity("pipeline_run", run_id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "run.started" in kinds
    assert "run.completed" in kinds

    states_seen = {e["state"] for e in received if e["run_id"] == str(run_id)}
    assert "running" in states_seen
    assert "completed" in states_seen


@pytest.mark.asyncio
async def test_action_error_fails_run_and_notifies_service(db_session, redis_or_skip) -> None:
    """An `ActionError` from a stage's action fails the run and notifies the
    kickoff actor."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_FailingAction())

        org_id, ticket_id, user_id = await _seed_org_ticket_and_user(db_session)
        pipeline_id = await create_pipeline(
            org_id=org_id,
            definition=_two_action_definition(["fail-action"]),
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

    run = (
        await db_session.execute(
            text("SELECT state, failure_reason FROM pipeline_runs WHERE id = :id"), {"id": run_id}
        )
    ).one()
    assert run.state == "failed"
    assert run.failure_reason == "boom"

    stage_row = (
        await db_session.execute(
            text("SELECT status, failure_reason FROM stage_executions WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
    ).one()
    assert stage_row.status == "failed"
    assert stage_row.failure_reason == "boom"

    entries = await list_for_entity("pipeline_run", run_id, org_id=org_id)
    assert "run.failed" in [e.kind for e in entries]

    notifications = await list_for_user(db_session, user_id=user_id, org_id=org_id)
    assert any(n.subject_id == run_id for n in notifications)
