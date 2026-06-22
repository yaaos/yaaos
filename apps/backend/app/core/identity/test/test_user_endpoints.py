"""Coverage for /api/user/emails — list/add/delete + last-verified guard."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.core.identity
import app.core.sessions  # noqa: F401  -- triggers auth route registration
from app.core.auth import AuthMiddleware, Role
from app.core.identity.repository import add_email, insert_user
from app.core.identity.sessions import create as _create_session
from app.domain.orgs import insert_membership, insert_org


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"user"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await insert_user(db_session, display_name="Acc")
    e1 = await add_email(db_session, user_id=user.id, email="primary@x.test", is_primary=True, verified=True)
    e2 = await add_email(db_session, user_id=user.id, email="alt@x.test", is_primary=False, verified=True)
    org = await insert_org(db_session, slug="acc-org")
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="acc")
    s = await _create_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"user": user, "e1": e1, "e2": e2, "org": org, "session": s}


@pytest.mark.asyncio
async def test_list_emails(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/user/emails",
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    emails = {row["email"] for row in body}
    assert emails == {"primary@x.test", "alt@x.test"}


@pytest.mark.asyncio
async def test_delete_non_last_verified_email_ok(seeded) -> None:
    async with _client() as c:
        r = await c.delete(
            f"/api/user/emails/{seeded['e2'].id}",
            cookies={
                "yaaos_session": seeded["session"].raw_token,
                "yaaos_csrf": seeded["session"].csrf_token,
            },
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["session"].csrf_token},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_delete_last_verified_email_blocked(db_session) -> None:
    user = await insert_user(db_session, display_name="One")
    only = await add_email(db_session, user_id=user.id, email="only@x.test", is_primary=True, verified=True)
    org = await insert_org(db_session, slug="one-org")
    await insert_membership(db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="one")
    s = await _create_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        r = await c.delete(
            f"/api/user/emails/{only.id}",
            cookies={"yaaos_session": s.raw_token, "yaaos_csrf": s.csrf_token},
            headers={"X-Yaaos-Org-Slug": org.slug, "X-CSRF-Token": s.csrf_token},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "last_verified_email"


@pytest.mark.asyncio
async def test_add_email_unverified(seeded) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/user/emails",
            json={"email": "third@x.test"},
            cookies={
                "yaaos_session": seeded["session"].raw_token,
                "yaaos_csrf": seeded["session"].csrf_token,
            },
            headers={"X-Yaaos-Org-Slug": seeded["org"].slug, "X-CSRF-Token": seeded["session"].csrf_token},
        )
    assert r.status_code == 200
    assert r.json()["verified"] is False
