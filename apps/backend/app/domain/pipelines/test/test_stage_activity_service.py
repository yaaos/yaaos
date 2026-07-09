"""Service test: `GET /api/pipelines/runs/{run_id}/stages/{stage_execution_id}/activity`.

Pattern mirrors `test_pipeline_crud_service.py`. The endpoint reuses
`core/coding_agent.get_stage_activity`, keyed on `coding_agent_runs.run_id` /
`stage_execution_id` (both UUID columns) — see `web.py`'s
`stage_activity_endpoint`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.coding_agent import ActivityEvent, ActivityLog, Usage, create_run, finalize_run
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


@pytest.mark.asyncio
async def test_stage_activity_returns_activity_after_uuid_rekey(seeded, db_session) -> None:
    """Post-rekey join: `coding_agent_runs.run_id` / `stage_execution_id` are
    both UUID columns — a real run row keyed on the path params' raw UUIDs
    (no stringify hop) resolves and its persisted activity blob comes back."""
    result = await seed_paused_run(org_slug=seeded["org"].slug, ticket_title="Activity join test")

    run_row_id = await create_run(
        org_id=seeded["org"].org_id,
        run_id=UUID(result["run_id"]),
        stage_execution_id=UUID(result["stage_execution_id"]),
        agent_command_id=uuid4(),
        command_kind="InvokeClaudeCode",
        plugin_id="claude_code",
        session=db_session,
    )
    await finalize_run(
        run_row_id,
        usage=Usage(tokens_in=10, tokens_out=20),
        duration_ms=1234,
        activity=ActivityLog(
            events=[
                ActivityEvent(
                    seq=0,
                    ts="2026-01-01T00:00:00Z",
                    kind="assistant_message",
                    message="done",
                )
            ]
        ),
        exit_code=0,
        status="success",
        session=db_session,
    )
    await db_session.commit()

    async with _client() as c:
        r = await c.get(
            f"/api/pipelines/runs/{result['run_id']}/stages/{result['stage_execution_id']}/activity",
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activity"]["events"][0]["message"] == "done"
