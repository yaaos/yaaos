"""Service test: `resume_stalled_runs` — the three idempotent reconciliations
that make Redis fully disposable for the run engine.

(a) a synthetic stale `running` run (no pending agent command, stale
    `updated_at`) is re-routed via `ROUTE_RUN` and runs to completion.
(b) a synthetic stale `running` run whose pending agent command already
    shows `status == "done"` (terminal event recorded, resume never
    processed) is re-routed via `HANDLE_AGENT_EVENT` as a loud failure.
(c) an orphaned `queued` run (no `running`/`paused` sibling on its ticket)
    is promoted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from uuid import uuid4, uuid7

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from app.core.audit_log import Actor
from app.core.tenancy import create_org
from app.domain.actions import ActionContext, register_action, set_actions_for_tests
from app.domain.pipelines import ActionStage, Kickoff
from app.domain.pipelines.models import PipelineRunRow, StageExecutionRow
from app.domain.pipelines.scheduler_jobs import resume_stalled_runs
from app.domain.pipelines.test.drain import drain
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.service

_STALE_DELTA = timedelta(seconds=600)  # older than the 300s default threshold


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


async def _seed_org_and_ticket(db_session) -> tuple[Any, Any]:
    org = await create_org(db_session, slug=f"stall-sweep-{uuid4().hex[:8]}", display_name="Stall Sweep Org")
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="stall sweep ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.flush()
    return org.org_id, ticket_id


def _kickoff_dict() -> dict:
    return Kickoff(intake_point_id="test", actor=Actor.system(), input_text=None).model_dump(mode="json")


@pytest.mark.asyncio
async def test_stale_running_run_reenqueues_route_run_service(db_session, redis_or_skip) -> None:
    """(a) A `running` run with no pending command and a stale `updated_at`
    is re-routed via `ROUTE_RUN` (bootstrap — no prior stage_executions row)
    and runs to completion once drained."""
    _RecordingAction.calls = []
    with set_actions_for_tests(scenario="empty"):
        register_action(_RecordingAction("stall-sweep-action"))

        org_id, ticket_id = await _seed_org_and_ticket(db_session)
        stale_at = datetime.now(UTC) - _STALE_DELTA

        run = PipelineRunRow(
            org_id=org_id,
            ticket_id=ticket_id,
            pipeline_id=None,
            pipeline_name="stall-sweep-pipeline",
            definition_snapshot={
                "stages": [ActionStage(action_id="stall-sweep-action").model_dump(mode="json")]
            },
            state="running",
            phase="stages",
            current_stage_index=None,
            workspace_id=None,
            pending_agent_command_id=None,
            kickoff=_kickoff_dict(),
            updated_at=stale_at,
        )
        db_session.add(run)
        await db_session.flush()
        run_id = run.id
        await db_session.commit()

        await resume_stalled_runs()
        await drain(db_session)

    row = (
        await db_session.execute(text("SELECT state FROM pipeline_runs WHERE id = :id"), {"id": run_id})
    ).one()
    assert row.state == "completed"
    assert _RecordingAction.calls == ["stall-sweep-action"]


@pytest.mark.asyncio
async def test_lost_resume_reenqueues_handle_agent_event_service(db_session, redis_or_skip) -> None:
    """(b) A `running` run whose pending agent command already shows
    `status == "done"` (terminal event recorded, resume never processed) is
    re-routed via `HANDLE_AGENT_EVENT` — the run fails loudly since the
    original outcome can't be recovered."""
    org_id, ticket_id = await _seed_org_and_ticket(db_session)
    stale_at = datetime.now(UTC) - _STALE_DELTA
    command_id = uuid7()

    run = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=None,
        pipeline_name="stall-sweep-pipeline",
        definition_snapshot={"stages": []},
        state="running",
        phase="provision",
        current_stage_index=0,
        workspace_id=None,
        pending_agent_command_id=command_id,
        kickoff=_kickoff_dict(),
        updated_at=stale_at,
    )
    db_session.add(run)
    await db_session.flush()
    run_id = run.id

    stage_exec = StageExecutionRow(
        org_id=org_id,
        run_id=run_id,
        stage_index=None,
        kind="system",
        stage_name="provision-workspace",
        status="running",
    )
    db_session.add(stage_exec)
    await db_session.flush()

    await db_session.execute(
        text(
            "INSERT INTO agent_commands "
            "(id, org_id, workflow_execution_id, command_kind, payload, status, attempt) "
            "VALUES (:id, :org_id, :run_id, 'InvokeClaudeCode', '{}'::jsonb, 'done', 0)"
        ),
        {"id": command_id, "org_id": org_id, "run_id": run_id},
    )
    await db_session.commit()

    await resume_stalled_runs()
    await drain(db_session)

    row = (
        await db_session.execute(
            text("SELECT state, failure_reason, pending_agent_command_id FROM pipeline_runs WHERE id = :id"),
            {"id": run_id},
        )
    ).one()
    assert row.state == "failed"
    assert row.pending_agent_command_id is None
    assert row.failure_reason is not None and "resume was never processed" in row.failure_reason


@pytest.mark.asyncio
async def test_orphaned_queued_run_is_promoted_service(db_session, redis_or_skip) -> None:
    """(c) A ticket with a `queued` run and no `running`/`paused` sibling is
    an orphaned promotion — the sweep promotes the oldest one."""
    org_id, ticket_id = await _seed_org_and_ticket(db_session)

    older = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=None,
        pipeline_name="stall-sweep-pipeline",
        definition_snapshot={"stages": []},
        state="queued",
        phase="stages",
        kickoff=_kickoff_dict(),
    )
    db_session.add(older)
    await db_session.flush()

    newer = PipelineRunRow(
        org_id=org_id,
        ticket_id=ticket_id,
        pipeline_id=None,
        pipeline_name="stall-sweep-pipeline",
        definition_snapshot={"stages": []},
        state="queued",
        phase="stages",
        kickoff=_kickoff_dict(),
    )
    db_session.add(newer)
    await db_session.flush()
    older_id, newer_id = older.id, newer.id
    assert older_id < newer_id, "uuid7 ids must sort chronologically for this assertion to be meaningful"
    await db_session.commit()

    await resume_stalled_runs()

    rows = (
        await db_session.execute(
            text("SELECT id, state FROM pipeline_runs WHERE id IN (:a, :b)"), {"a": older_id, "b": newer_id}
        )
    ).all()
    state_by_id = {r.id: r.state for r in rows}
    assert state_by_id[older_id] == "running"
    assert state_by_id[newer_id] == "queued"
