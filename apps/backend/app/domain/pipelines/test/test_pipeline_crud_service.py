"""Service test: pipeline-definition CRUD over `/api/pipelines`, driven via
`httpx.ASGITransport` — create/read/update/delete, cycle + name-collision +
referenced-delete rejection, role gating, and audit rows.

Pattern mirrors `app/domain/integrations/test/test_endpoints.py`.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.audit_log import list_for_org
from app.core.auth import AuthMiddleware, Role
from app.core.identity import create_user, mint_session
from app.domain.orgs import insert_membership, insert_org
from app.domain.pipelines import web as _pipelines_web  # noqa: F401 -- triggers route registration

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
    admin = await create_user(db_session, display_name="A")
    member = await create_user(db_session, display_name="M")
    org = await insert_org(db_session, slug=f"pipe-{uuid4().hex[:8]}")
    await insert_membership(db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm")
    await insert_membership(db_session, user_id=member.id, org_id=org.org_id, role=Role.BUILDER, handle="mem")
    admin_sess = await mint_session(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await mint_session(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "admin": admin,
        "admin_sess": admin_sess,
        "member_sess": member_sess,
    }


def _admin_headers(seeded, *, mutate: bool = False) -> dict[str, str]:
    headers = {"X-Yaaos-Org-Slug": seeded["org"].slug}
    if mutate:
        headers["X-CSRF-Token"] = seeded["admin_sess"].csrf_token
    return headers


def _admin_cookies(seeded) -> dict[str, str]:
    return {
        "yaaos_session": seeded["admin_sess"].raw_token,
        "yaaos_csrf": seeded["admin_sess"].csrf_token,
    }


def _two_stage_definition(name: str) -> dict:
    return {
        "name": name,
        "stages": [
            {
                "kind": "skill",
                "name": "spec",
                "skill_name": "write-spec",
                "coding_agent_plugin_id": "claude_code",
                "model": "sonnet",
                "effort": "medium",
                "boundary": {"mode": "always_hitl"},
            },
            {
                "kind": "skill",
                "name": "implement",
                "skill_name": "implement",
                "coding_agent_plugin_id": "claude_code",
                "model": "sonnet",
                "effort": "high",
                "boundary": {"mode": "always_proceed"},
            },
        ],
    }


def _one_stage_definition(name: str) -> dict:
    return {
        "name": name,
        "stages": [
            {
                "kind": "skill",
                "name": "spec",
                "skill_name": "write-spec",
                "coding_agent_plugin_id": "claude_code",
                "model": "sonnet",
                "effort": "medium",
                "boundary": {"mode": "always_hitl"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_create_returns_201_and_get_round_trips(seeded) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/pipelines",
            json=_two_stage_definition("build-and-ship"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert r.status_code == 201, r.text
        pipeline_id = r.json()["id"]

        got = await c.get(
            f"/api/pipelines/{pipeline_id}",
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded),
        )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["id"] == pipeline_id
    assert body["name"] == "build-and-ship"
    assert [s["name"] for s in body["stages"]] == ["spec", "implement"]
    assert body["updated_by_login"] == "adm"
    assert body["referenced"] is False


@pytest.mark.asyncio
async def test_create_with_self_referential_call_cycle_returns_400(seeded) -> None:
    cyclic_id = str(uuid4())
    body = {
        "id": cyclic_id,
        "name": "cyclic",
        "stages": [{"kind": "call", "pipeline_id": cyclic_id}],
    }
    async with _client() as c:
        r = await c.post(
            "/api/pipelines",
            json=body,
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "invalid_definition"


@pytest.mark.asyncio
async def test_create_with_duplicate_name_returns_409(seeded) -> None:
    async with _client() as c:
        first = await c.post(
            "/api/pipelines",
            json=_one_stage_definition("dup-name"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert first.status_code == 201, first.text

        second = await c.post(
            "/api/pipelines",
            json=_one_stage_definition("dup-name"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["error"] == "name_taken"


@pytest.mark.asyncio
async def test_delete_referenced_pipeline_returns_409(seeded) -> None:
    async with _client() as c:
        callee = await c.post(
            "/api/pipelines",
            json=_one_stage_definition("callee"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert callee.status_code == 201, callee.text
        callee_id = callee.json()["id"]

        caller_body = {
            "name": "caller",
            "stages": [{"kind": "call", "pipeline_id": callee_id}],
        }
        caller = await c.post(
            "/api/pipelines",
            json=caller_body,
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert caller.status_code == 201, caller.text

        deleted = await c.delete(
            f"/api/pipelines/{callee_id}",
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["detail"]["error"] == "referenced"


@pytest.mark.asyncio
async def test_delete_unreferenced_pipeline_returns_204(seeded) -> None:
    async with _client() as c:
        created = await c.post(
            "/api/pipelines",
            json=_one_stage_definition("solo"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert created.status_code == 201, created.text
        pipeline_id = created.json()["id"]

        deleted = await c.delete(
            f"/api/pipelines/{pipeline_id}",
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert deleted.status_code == 204, deleted.text

        missing = await c.get(
            f"/api/pipelines/{pipeline_id}",
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded),
        )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_update_replaces_definition(seeded) -> None:
    async with _client() as c:
        created = await c.post(
            "/api/pipelines",
            json=_one_stage_definition("evolve"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert created.status_code == 201, created.text
        pipeline_id = created.json()["id"]

        updated = await c.put(
            f"/api/pipelines/{pipeline_id}",
            json=_two_stage_definition("evolve"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
    assert updated.status_code == 200, updated.text
    assert [s["name"] for s in updated.json()["stages"]] == ["spec", "implement"]


@pytest.mark.asyncio
async def test_get_unknown_pipeline_404(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            f"/api/pipelines/{uuid4()}",
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded),
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_returns_summaries(seeded) -> None:
    async with _client() as c:
        await c.post(
            "/api/pipelines",
            json=_one_stage_definition("alpha"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        await c.post(
            "/api/pipelines",
            json=_two_stage_definition("beta"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        r = await c.get(
            "/api/pipelines",
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded),
        )
    assert r.status_code == 200, r.text
    summaries = {p["name"]: p for p in r.json()["pipelines"]}
    assert summaries["alpha"]["stage_count"] == 1
    assert summaries["beta"]["stage_count"] == 2
    assert summaries["alpha"]["updated_by_login"] == "adm"


@pytest.mark.asyncio
async def test_list_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/pipelines", headers={"X-Yaaos-Org-Slug": seeded["org"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_builder_member_forbidden_403(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/pipelines",
            cookies={"yaaos_session": seeded["member_sess"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_create_and_delete_write_audit_rows(seeded) -> None:
    async with _client() as c:
        created = await c.post(
            "/api/pipelines",
            json=_one_stage_definition("audited"),
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert created.status_code == 201, created.text
        pipeline_id = created.json()["id"]

        deleted = await c.delete(
            f"/api/pipelines/{pipeline_id}",
            cookies=_admin_cookies(seeded),
            headers=_admin_headers(seeded, mutate=True),
        )
        assert deleted.status_code == 204, deleted.text

    entries = await list_for_org(org_id=seeded["org"].org_id)
    kinds = [e.kind for e in entries]
    assert "pipeline.created" in kinds
    assert "pipeline.deleted" in kinds
