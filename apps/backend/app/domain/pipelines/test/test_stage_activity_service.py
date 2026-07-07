"""Service test: `GET /api/pipelines/runs/{run_id}/stages/{stage_execution_id}/activity`.

Pattern mirrors `test_pipeline_crud_service.py`. The endpoint reuses
`core/coding_agent.get_step_activity`, keyed on the pipelines engine's reuse
of `coding_agent_runs.workflow_execution_id`/`step_id` (TEXT, pre-rename) —
see `web.py`'s `stage_activity_endpoint`.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.identity import create_user, mint_session
from app.domain.orgs import insert_membership, insert_org
from app.domain.pipelines import web as _pipelines_web  # noqa: F401 -- triggers route registration
from app.testing.e2e_setup import seed_paused_run

pytestmark = pytest.mark.service


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
    member = await create_user(db_session, display_name="M")
    org = await insert_org(db_session, slug=f"activity-{uuid4().hex[:8]}")
    await insert_membership(db_session, user_id=member.id, org_id=org.org_id, role=Role.BUILDER, handle="mem")
    member_sess = await mint_session(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    return {"org": org, "member_sess": member_sess}


def _headers(seeded) -> dict[str, str]:
    return {"X-Yaaos-Org-Slug": seeded["org"].slug}


def _cookies(seeded) -> dict[str, str]:
    return {"yaaos_session": seeded["member_sess"].raw_token, "yaaos_csrf": seeded["member_sess"].csrf_token}


@pytest.mark.asyncio
async def test_stage_activity_returns_null_when_no_coding_agent_run(seeded) -> None:
    """A seeded stage execution never dispatched a real invocation — the
    endpoint 200s with `activity: null` rather than 404ing."""
    result = await seed_paused_run(org_slug=seeded["org"].slug, ticket_title="Activity read test")

    async with _client() as c:
        r = await c.get(
            f"/api/pipelines/runs/{result['run_id']}/stages/{result['stage_execution_id']}/activity",
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"activity": None}


@pytest.mark.asyncio
async def test_stage_activity_404s_for_run_in_another_org(seeded, db_session) -> None:
    """Cross-tenant safety: a run belonging to a different org 404s even
    though the run/stage-execution ids themselves are valid."""
    other_org = await insert_org(db_session, slug=f"activity-other-{uuid4().hex[:8]}")
    await db_session.commit()
    other_result = await seed_paused_run(org_slug=other_org.slug, ticket_title="Other org's run")

    async with _client() as c:
        r = await c.get(
            f"/api/pipelines/runs/{other_result['run_id']}/stages/"
            f"{other_result['stage_execution_id']}/activity",
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"] == "not_found"
