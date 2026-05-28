"""Coverage for the user surface: GET/PATCH /api/user/me + the
membership handle update endpoint. The GitHub username denorm is owned by
the login flow now; there's no verify-only flow to test here."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.identity import user_web as _user_web  # noqa: F401
from app.domain.orgs import Role
from app.domain.orgs import repository as orgs_repo
from app.domain.sessions import web as _auth_web  # noqa: F401


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
    user = await identity_repo.insert_user(db_session, display_name="Acc")
    await identity_repo.add_email(
        db_session, user_id=user.id, email="primary@x.test", is_primary=True, verified=True
    )
    org_a = await orgs_repo.insert_org(db_session, slug="org-a")
    org_b = await orgs_repo.insert_org(db_session, slug="org-b")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.id, role=Role.BUILDER, handle="alpha"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_b.id, role=Role.BUILDER, handle="beta"
    )
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"user": user, "org_a": org_a, "org_b": org_b, "session": s}


# ── GET /api/user/me ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_me_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/user/me", headers={"X-Org-Slug": seeded["org_a"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_user_me_returns_memberships_and_handles(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/user/me",
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org_a"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Acc"
    assert body["github_username"] is None
    handles = {m["slug"]: m["handle"] for m in body["memberships"]}
    assert handles == {"org-a": "alpha", "org-b": "beta"}


@pytest.mark.asyncio
async def test_user_me_works_without_org_slug_header(seeded) -> None:
    """`/api/user/me` is USER_SCOPED — the middleware must let the request
    through without `X-Org-Slug`. Whether the SPA happens to be on an
    org-scoped URL when calling it (which would attach the header) is
    irrelevant; the route ignores it."""
    async with _client() as c:
        r = await c.get(
            "/api/user/me",
            cookies={"yaaos_session": seeded["session"].raw_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Acc"
    assert {m["slug"] for m in body["memberships"]} == {"org-a", "org-b"}


@pytest.mark.asyncio
async def test_user_me_anonymous_without_header_is_401(seeded) -> None:
    """No session, no header → 401 from `require_session`, not 400 from
    the middleware. The route is USER_SCOPED, not ORG_SCOPED."""
    async with _client() as c:
        r = await c.get("/api/user/me")
    assert r.status_code == 401


# ── PATCH /api/user/me ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_user_updates_display_name(seeded) -> None:
    sess = seeded["session"]
    async with _client() as c:
        r = await c.patch(
            "/api/user/me",
            json={"display_name": "New Name"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org_a"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "New Name"


@pytest.mark.asyncio
async def test_patch_clears_github_username(seeded, db_session) -> None:
    await identity_repo.set_user_github_username(
        db_session, user_id=seeded["user"].id, github_username="octocat"
    )
    await db_session.commit()
    sess = seeded["session"]
    async with _client() as c:
        r = await c.patch(
            "/api/user/me",
            json={"clear_github_username": True},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org_a"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["github_username"] is None


# ── PATCH /api/memberships/me/{org_id} ──────────────────────────────────────


def _memberships_app() -> FastAPI:
    from app.domain.orgs import web as _orgs_web  # noqa: F401, PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"memberships"})
    return app


def _memberships_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_memberships_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_patch_own_handle_updates(seeded) -> None:
    sess = seeded["session"]
    async with _memberships_client() as c:
        r = await c.patch(
            f"/api/memberships/me/{seeded['org_a'].id}",
            json={"handle": "renamed"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org_a"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["handle"] == "renamed"


@pytest.mark.asyncio
async def test_patch_own_handle_rejects_duplicate(seeded, db_session) -> None:
    # Add another member to org_a holding the handle we'll try to take.
    other = await identity_repo.insert_user(db_session, display_name="Other")
    await orgs_repo.insert_membership(
        db_session,
        user_id=other.id,
        org_id=seeded["org_a"].id,
        role=Role.BUILDER,
        handle="taken",
    )
    await db_session.commit()
    sess = seeded["session"]
    async with _memberships_client() as c:
        r = await c.patch(
            f"/api/memberships/me/{seeded['org_a'].id}",
            json={"handle": "taken"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org_a"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_patch_own_handle_rejects_blank(seeded) -> None:
    sess = seeded["session"]
    async with _memberships_client() as c:
        r = await c.patch(
            f"/api/memberships/me/{seeded['org_a'].id}",
            json={"handle": "  "},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org_a"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_own_handle_rejects_membership_not_found(seeded) -> None:
    from uuid import uuid4  # noqa: PLC0415

    sess = seeded["session"]
    async with _memberships_client() as c:
        r = await c.patch(
            f"/api/memberships/me/{uuid4()}",
            json={"handle": "x"},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org_a"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 404
