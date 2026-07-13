"""Service test: repo trigger-binding CRUD over `/api/repos/triggers`, driven
via `httpx.ASGITransport` — validation errors (unknown intake point, unknown
pipeline, duplicate binding), successful add/remove, and the
`domain.pipelines.delete_pipeline` 409 a live binding produces via
`pipeline_referenced_by_binding`.

Pattern mirrors `app/domain/pipelines/test/test_pipeline_crud_service.py`.
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
from app.domain.repos import web as _repos_web  # noqa: F401 -- triggers route registration
from app.domain.repos.models import RepoTriggerBindingRow

pytestmark = pytest.mark.service


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"repos", "pipelines"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    admin = await create_user(db_session, display_name="A")
    member = await create_user(db_session, display_name="M")
    org = await insert_org(db_session, slug=f"repo-bind-{uuid4().hex[:8]}")
    await insert_membership(db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm")
    await insert_membership(db_session, user_id=member.id, org_id=org.org_id, role=Role.BUILDER, handle="mem")
    admin_sess = await mint_session(db_session, user_id=admin.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "admin_sess": admin_sess, "admin_id": admin.id, "member_id": member.id}


def _headers(seeded, *, mutate: bool = False) -> dict[str, str]:
    headers = {"X-Yaaos-Org-Slug": seeded["org"].slug}
    if mutate:
        headers["X-CSRF-Token"] = seeded["admin_sess"].csrf_token
    return headers


def _cookies(seeded) -> dict[str, str]:
    return {
        "yaaos_session": seeded["admin_sess"].raw_token,
        "yaaos_csrf": seeded["admin_sess"].csrf_token,
    }


def _one_stage_definition(name: str) -> dict:
    return {"name": name, "stages": [{"kind": "action", "action_id": "github:create_pr"}]}


async def _create_pipeline_via_http(c: httpx.AsyncClient, seeded, name: str) -> str:
    r = await c.post(
        "/api/pipelines",
        json=_one_stage_definition(name),
        cookies=_cookies(seeded),
        headers=_headers(seeded, mutate=True),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_add_binding_success_returns_201(seeded) -> None:
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "bindable")
        r = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 201, r.text
    assert "id" in r.json()


@pytest.mark.asyncio
async def test_add_binding_unknown_point_returns_400(seeded) -> None:
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "unknown-point-target")
        r = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "not_a_real_point", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "unknown_point"


@pytest.mark.asyncio
async def test_add_binding_unowned_pipeline_returns_404(seeded) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": str(uuid4())},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"] == "pipeline_not_found"


@pytest.mark.asyncio
async def test_add_binding_duplicate_returns_409(seeded) -> None:
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "dup-target")
        first = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
        assert first.status_code == 201, first.text

        second = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "duplicate_binding"


@pytest.mark.asyncio
async def test_add_binding_invalid_cron_returns_400(seeded) -> None:
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "cron-target")
        r = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={
                "intake_point_id": "schedule",
                "pipeline_id": pipeline_id,
                "schedule": {
                    "name": "nightly",
                    "cron": "not a cron",
                    "notify_user_ids": [str(seeded["admin_id"])],
                    "kickoff_input": None,
                },
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "invalid_cron"


@pytest.mark.asyncio
async def test_add_binding_empty_notify_user_ids_returns_400(seeded) -> None:
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "empty-notify-target")
        r = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={
                "intake_point_id": "schedule",
                "pipeline_id": pipeline_id,
                "schedule": {
                    "name": "nightly",
                    "cron": "0 0 * * *",
                    "notify_user_ids": [],
                    "kickoff_input": None,
                },
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_add_binding_schedule_for_webhook_point_returns_400(seeded) -> None:
    """A `schedule` payload on a non-schedule-kind point is rejected."""
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "webhook-with-schedule")
        r = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={
                "intake_point_id": "github:pr_opened",
                "pipeline_id": pipeline_id,
                "schedule": {
                    "name": "nightly",
                    "cron": "0 0 * * *",
                    "notify_user_ids": [str(seeded["admin_id"])],
                    "kickoff_input": None,
                },
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_remove_binding_returns_204_then_404(seeded) -> None:
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "removable")
        added = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
        binding_id = added.json()["id"]

        first_delete = await c.delete(
            f"/api/repos/triggers/{binding_id}",
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
        assert first_delete.status_code == 204, first_delete.text

        second_delete = await c.delete(
            f"/api/repos/triggers/{binding_id}",
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert second_delete.status_code == 404, second_delete.text


@pytest.mark.asyncio
async def test_get_repo_config_lists_binding(seeded) -> None:
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "listed-in-config")
        await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
        r = await c.get(
            "/api/repos/config",
            params={"repo": "acme/web"},
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["repo_external_id"] == "acme/web"
    assert len(body["bindings"]) == 1
    assert body["bindings"][0]["pipeline_id"] == pipeline_id
    assert body["bindings"][0]["pipeline_name"] == "listed-in-config"


@pytest.mark.asyncio
async def test_delete_pipeline_referenced_by_binding_returns_409(seeded) -> None:
    """The Phase-1 stub (`pipeline_referenced_by_binding` always `False`) is
    retired — a live binding now blocks the delete."""
    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "referenced-by-binding")
        bound = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
        assert bound.status_code == 201, bound.text

        deleted = await c.delete(
            f"/api/pipelines/{pipeline_id}",
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"]["error"] == "referenced"


@pytest.mark.asyncio
async def test_add_binding_stamps_created_by_with_acting_user(seeded, db_session) -> None:
    """POST /api/repos/triggers stamps created_by on the binding row with the acting user's ID."""
    from sqlalchemy import select  # noqa: PLC0415

    admin_id = seeded["admin_id"]

    async with _client() as c:
        pipeline_id = await _create_pipeline_via_http(c, seeded, "created-by-stamped")
        resp = await c.post(
            "/api/repos/triggers",
            params={"repo": "acme/web"},
            json={"intake_point_id": "github:pr_opened", "pipeline_id": pipeline_id},
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert resp.status_code == 201, resp.text
    binding_id = resp.json()["id"]

    row = (
        await db_session.execute(select(RepoTriggerBindingRow).where(RepoTriggerBindingRow.id == binding_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.created_by == admin_id, f"created_by must be the acting admin; got {row.created_by!r}"
