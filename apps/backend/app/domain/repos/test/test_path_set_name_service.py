"""Service tests: `ProtectedPathSet.name` round-trips through the repo-config
save/load cycle and legacy JSONB without a `name` key parses to `""`.

Pattern mirrors `test_repo_bindings_service.py` — httpx.ASGITransport.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import text

from app.core.auth import AuthMiddleware, Role
from app.core.database import session as db_session
from app.core.identity import create_user, mint_session
from app.domain.orgs import insert_membership, insert_org
from app.domain.repos import web as _repos_web  # noqa: F401 -- triggers route registration

pytestmark = pytest.mark.service


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"repos"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    admin = await create_user(db_session, display_name="A")
    org = await insert_org(db_session, slug=f"path-set-name-{uuid4().hex[:8]}")
    await insert_membership(db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="adm")
    admin_sess = await mint_session(db_session, user_id=admin.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "admin_sess": admin_sess}


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


@pytest.mark.asyncio
async def test_name_round_trips_through_save_and_load(seeded) -> None:
    """PUT a path set with a non-empty name; GET reads it back unchanged."""
    path_set_id = str(uuid4())
    async with _client() as c:
        r = await c.put(
            "/api/repos/settings",
            params={"repo": "acme/web"},
            json={
                "protected_mode": "deny",
                "protected_path_sets": [
                    {
                        "id": path_set_id,
                        "name": "Infra paths",
                        "globs": ["infra/**"],
                        "owner_user_ids": [],
                    }
                ],
                "auto_approve_enabled": False,
                "auto_approve_conditions": {},
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
        assert r.status_code == 200, r.text

        r2 = await c.get(
            "/api/repos/config",
            params={"repo": "acme/web"},
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r2.status_code == 200, r2.text
    sets = r2.json()["protected_path_sets"]
    assert len(sets) == 1
    assert sets[0]["name"] == "Infra paths"
    assert sets[0]["globs"] == ["infra/**"]


@pytest.mark.asyncio
async def test_legacy_jsonb_without_name_parses_to_empty_string(seeded) -> None:
    """JSONB stored before the `name` field existed (no `name` key) must
    parse to `""` via Pydantic's `default=""`."""
    org_id = seeded["org"].org_id
    path_set_id = str(uuid4())
    # Write a JSONB row directly — bypassing the service layer — to simulate
    # a pre-migration row with no `name` key.
    async with db_session() as s:
        await s.execute(
            text(
                "INSERT INTO repo_settings "
                "(org_id, repo_external_id, protected_mode, protected_path_sets, "
                " auto_approve_enabled, auto_approve_conditions) "
                "VALUES (:org_id, :repo, 'deny', CAST(:path_sets AS jsonb), false, CAST(:cond AS jsonb))"
            ),
            {
                "org_id": str(org_id),
                "repo": "acme/legacy",
                "path_sets": f'[{{"id": "{path_set_id}", "globs": ["src/**"], "owner_user_ids": []}}]',
                "cond": "{}",
            },
        )
        await s.commit()

    async with _client() as c:
        r = await c.get(
            "/api/repos/config",
            params={"repo": "acme/legacy"},
            cookies=_cookies(seeded),
            headers=_headers(seeded),
        )
    assert r.status_code == 200, r.text
    sets = r.json()["protected_path_sets"]
    assert len(sets) == 1
    assert sets[0]["name"] == ""


@pytest.mark.asyncio
async def test_name_max_length_100_enforced(seeded) -> None:
    """A `name` longer than 100 characters is rejected with a validation error."""
    async with _client() as c:
        r = await c.put(
            "/api/repos/settings",
            params={"repo": "acme/web"},
            json={
                "protected_mode": "deny",
                "protected_path_sets": [
                    {
                        "id": str(uuid4()),
                        "name": "x" * 101,
                        "globs": ["infra/**"],
                        "owner_user_ids": [],
                    }
                ],
                "auto_approve_enabled": False,
                "auto_approve_conditions": {},
            },
            cookies=_cookies(seeded),
            headers=_headers(seeded, mutate=True),
        )
    assert r.status_code == 422, r.text
