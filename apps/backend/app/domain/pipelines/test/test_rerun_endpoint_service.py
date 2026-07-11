"""Service test: `POST /api/pipelines/runs/{run_id}/rerun`, driven via
`httpx.ASGITransport`, plus `RunOutcome.run_id` on the terminal Overview
payload.

Pattern mirrors `test_pipeline_crud_service.py` / `test_stage_activity_service.py`.
Uses action-stage-only pipelines (no coding-agent/workspace stub needed) —
`ActionError` drives a run to `failed`; a succeeding action drives a run to
`completed`.
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID, uuid4, uuid7

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import text

from app.core.audit_log import Actor, ActorKind
from app.core.auth import AuthMiddleware, Role, org_context
from app.core.identity import create_user, mint_session
from app.domain.actions import ActionContext, ActionError, register_action, set_actions_for_tests
from app.domain.orgs import insert_membership, insert_org
from app.domain.pipelines import (
    ActionStage,
    Kickoff,
    PipelineDefinition,
    create_pipeline,
    start_run,
)
from app.domain.pipelines import web as _pipelines_web  # noqa: F401 -- triggers route registration
from app.domain.pipelines.models import PipelineRunRow
from app.domain.pipelines.test.drain import drain
from app.domain.pipelines.views import get_run_overview
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


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"pipelines"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    admin = await create_user(db_session, display_name="A")
    org = await insert_org(db_session, slug=f"rerun-{uuid4().hex[:8]}")
    await insert_membership(db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm")
    admin_sess = await mint_session(db_session, user_id=admin.id, workspace_id=None)
    await db_session.commit()
    return {"org": org, "admin": admin, "admin_sess": admin_sess}


def _headers(seeded) -> dict[str, str]:
    return {"X-Yaaos-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["admin_sess"].csrf_token}


def _cookies(seeded) -> dict[str, str]:
    return {"yaaos_session": seeded["admin_sess"].raw_token, "yaaos_csrf": seeded["admin_sess"].csrf_token}


def _failing_definition() -> PipelineDefinition:
    return PipelineDefinition(name=f"pipe-{uuid4().hex[:8]}", stages=(ActionStage(action_id="fail-action"),))


def _succeeding_definition(action_id: str) -> PipelineDefinition:
    return PipelineDefinition(name=f"pipe-{uuid4().hex[:8]}", stages=(ActionStage(action_id=action_id),))


async def _seed_run(seeded, db_session, *, definition: PipelineDefinition) -> UUID:
    ticket_id, _ = await create_from_pr(
        org_id=seeded["org"].org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="rerun endpoint test ticket",
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
    pipeline_id = await create_pipeline(
        org_id=seeded["org"].org_id, definition=definition, actor=Actor.system(), session=db_session
    )
    await db_session.flush()

    kickoff = Kickoff(
        intake_point_id="test", actor=Actor.user(user_id=seeded["admin"].id), input_text="do it"
    )
    run_id = await start_run(
        org_id=seeded["org"].org_id,
        ticket_id=ticket_id,
        pipeline_id=pipeline_id,
        kickoff=kickoff,
        session=db_session,
    )
    await db_session.commit()
    await drain(db_session)
    return run_id


@pytest.mark.asyncio
async def test_rerun_endpoint_starts_new_run_and_run_outcome_carries_run_id_service(
    seeded, db_session, redis_or_skip
) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_FailingAction())
        run_id = await _seed_run(seeded, db_session, definition=_failing_definition())

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "failed"

    # `RunOutcome.run_id` on the terminal Overview payload carries the
    # failed run's own id — the SPA's rerun button needs it.
    async with org_context(seeded["org"].org_id, ActorKind.SYSTEM):
        overview = await get_run_overview(run.ticket_id, session=db_session)
    assert overview is not None
    assert overview.status == "terminal"
    assert overview.outcome is not None
    assert overview.outcome.run_id == run_id

    async with _client() as c:
        r = await c.post(
            f"/api/pipelines/runs/{run_id}/rerun",
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 201, r.text
    new_run_id = UUID(r.json()["run_id"])
    assert new_run_id != run_id

    new_run = await db_session.get(PipelineRunRow, new_run_id)
    assert new_run is not None
    assert new_run.ticket_id == run.ticket_id


@pytest.mark.asyncio
async def test_rerun_endpoint_404s_for_unknown_run_service(seeded) -> None:
    async with _client() as c:
        r = await c.post(
            f"/api/pipelines/runs/{uuid7()}/rerun",
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"] == "not_found"


@pytest.mark.asyncio
async def test_rerun_endpoint_409s_for_non_rerunnable_run_service(seeded, db_session, redis_or_skip) -> None:
    with set_actions_for_tests(scenario="empty"):
        register_action(_RecordingAction("note-a"))
        run_id = await _seed_run(seeded, db_session, definition=_succeeding_definition("note-a"))

    run = await db_session.get(PipelineRunRow, run_id)
    assert run is not None
    assert run.state == "completed"

    async with _client() as c:
        r = await c.post(
            f"/api/pipelines/runs/{run_id}/rerun",
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "run_not_rerunnable"
