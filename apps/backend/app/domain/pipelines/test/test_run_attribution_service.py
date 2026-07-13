"""Service tests: run attribution stamping.

Verifies that `triggered_by_user_id` is persisted on the `pipeline_runs` row:
- `start_run` with a user ID stamps the column.
- `start_run` without a user ID leaves it NULL.
- The rerun endpoint stamps the acting session user.

Uses action-stage pipelines only (no workspace/coding-agent stub needed).
"""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.auth import AuthMiddleware, Role
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
from app.domain.tickets import create_from_pr

pytestmark = pytest.mark.service


# ── Actions ────────────────────────────────────────────────────────────────────


class _NopResult(BaseModel):
    ok: bool = True


class _NopAction:
    action_id = "nop-attr-action"
    plugin_id: str | None = None
    label = "Nop test action"
    Result: ClassVar[type[BaseModel]] = _NopResult

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        return _NopResult()


class _FailResult(BaseModel):
    ok: bool = False


class _FailAction:
    action_id = "fail-attr-action"
    plugin_id: str | None = None
    label = "Failing test action"
    Result: ClassVar[type[BaseModel]] = _FailResult

    async def execute(self, ctx: ActionContext, *, session: Any) -> BaseModel:
        del ctx, session
        raise ActionError("deliberate failure")


# ── App + client helpers ───────────────────────────────────────────────────────


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"pipelines"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


# ── Fixture ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession):
    admin = await create_user(db_session, display_name="Admin")
    org = await insert_org(db_session, slug=f"attr-{uuid4().hex[:8]}")
    await insert_membership(db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm")
    admin_sess = await mint_session(db_session, user_id=admin.id, workspace_id=None)
    await db_session.commit()
    return {"org": org, "admin": admin, "admin_sess": admin_sess}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _nop_definition() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"attr-pipe-{uuid4().hex[:8]}",
        stages=(ActionStage(action_id=_NopAction.action_id),),
    )


def _fail_definition() -> PipelineDefinition:
    return PipelineDefinition(
        name=f"attr-fail-{uuid4().hex[:8]}",
        stages=(ActionStage(action_id=_FailAction.action_id),),
    )


async def _seed_ticket(seeded, db_session: AsyncSession) -> UUID:
    ticket_id, _ = await create_from_pr(
        org_id=seeded["org"].org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="attribution test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.execute(
        text("UPDATE tickets SET branch_name = :b WHERE id = :id"),
        {"b": "yaaos/test-attr", "id": ticket_id},
    )
    await db_session.flush()
    return ticket_id


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_run_stamps_triggered_by_user_id(seeded, db_session: AsyncSession) -> None:
    """start_run with a triggered_by_user_id → column persisted on the run row."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NopAction())

        ticket_id = await _seed_ticket(seeded, db_session)
        pipeline_id = await create_pipeline(
            org_id=seeded["org"].org_id,
            definition=_nop_definition(),
            actor=Actor.system(),
            session=db_session,
        )
        attributed_user_id = seeded["admin"].id
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text="go")
        run_id = await start_run(
            org_id=seeded["org"].org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            triggered_by_user_id=attributed_user_id,
            session=db_session,
        )
        await db_session.commit()

    row = await db_session.get(PipelineRunRow, run_id)
    assert row is not None
    assert row.triggered_by_user_id == attributed_user_id, (
        f"triggered_by_user_id must be persisted; got {row.triggered_by_user_id!r}"
    )


@pytest.mark.asyncio
async def test_start_run_without_attribution_leaves_null(seeded, db_session: AsyncSession) -> None:
    """start_run without triggered_by_user_id → column is NULL."""
    with set_actions_for_tests(scenario="empty"):
        register_action(_NopAction())

        ticket_id = await _seed_ticket(seeded, db_session)
        pipeline_id = await create_pipeline(
            org_id=seeded["org"].org_id,
            definition=_nop_definition(),
            actor=Actor.system(),
            session=db_session,
        )
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text="go")
        run_id = await start_run(
            org_id=seeded["org"].org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()

    row = await db_session.get(PipelineRunRow, run_id)
    assert row is not None
    assert row.triggered_by_user_id is None, (
        f"triggered_by_user_id must be NULL when not supplied; got {row.triggered_by_user_id!r}"
    )


@pytest.mark.asyncio
async def test_rerun_endpoint_stamps_acting_user(seeded, db_session: AsyncSession, redis_or_skip) -> None:
    """POST /api/pipelines/runs/{run_id}/rerun stamps triggered_by_user_id with the acting user.

    Uses a failing action so the initial run reaches `failed` (a rerunnable
    terminal state) via drain, then calls the rerun endpoint as the admin user
    and asserts the new run row carries the admin's user id.
    """
    with set_actions_for_tests(scenario="empty"):
        register_action(_FailAction())

        ticket_id = await _seed_ticket(seeded, db_session)
        pipeline_id = await create_pipeline(
            org_id=seeded["org"].org_id,
            definition=_fail_definition(),
            actor=Actor.system(),
            session=db_session,
        )
        kickoff = Kickoff(intake_point_id="test", actor=Actor.system(), input_text="initial")
        run_id = await start_run(
            org_id=seeded["org"].org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()
        await drain(db_session)

    initial_row = await db_session.get(PipelineRunRow, run_id)
    assert initial_row is not None
    assert initial_row.state == "failed"

    admin_id = seeded["admin"].id
    admin_sess = seeded["admin_sess"]
    org_slug = seeded["org"].slug

    async with _client() as c:
        with set_actions_for_tests(scenario="empty"):
            register_action(_NopAction())
            resp = await c.post(
                f"/api/pipelines/runs/{run_id}/rerun",
                headers={
                    "X-Yaaos-Org-Slug": org_slug,
                    "X-CSRF-Token": admin_sess.csrf_token,
                },
                cookies={
                    "yaaos_session": admin_sess.raw_token,
                    "yaaos_csrf": admin_sess.csrf_token,
                },
            )
    assert resp.status_code == 201, f"Rerun endpoint returned {resp.status_code}: {resp.text}"

    rerun_id = resp.json()["run_id"]
    await db_session.commit()

    rerun_row = (
        await db_session.execute(select(PipelineRunRow).where(PipelineRunRow.id == rerun_id))
    ).scalar_one_or_none()
    assert rerun_row is not None, "Rerun row not found"
    assert rerun_row.triggered_by_user_id == admin_id, (
        f"Rerun must stamp acting user; got {rerun_row.triggered_by_user_id!r}"
    )
