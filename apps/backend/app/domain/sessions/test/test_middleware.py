"""Integration tests for `core/auth` middleware + `domain/sessions` dependencies.

The ad-hoc FastAPI app is driven by `httpx.AsyncClient` over an ASGI
transport so requests stay on the test's event loop (asyncpg refuses to
straddle loops).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from app.core.auth import AuthMiddleware
from app.core.auth.types import Action
from app.domain.identity import repository as identity_repo
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role
from app.domain.sessions import public_route, require


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/memberships/ok", dependencies=[Depends(require(Action.MEMBERS_READ))])
    async def ok() -> dict[str, str]:
        return {"ok": "yes"}

    @app.delete(
        "/api/memberships/admin-only",
        dependencies=[Depends(require(Action.MEMBERS_REMOVE))],
    )
    async def admin_only() -> dict[str, str]:
        return {"ok": "admin"}

    @app.post(
        "/api/memberships/mutate",
        dependencies=[Depends(require(Action.MEMBERS_INVITE))],
    )
    async def mutate() -> dict[str, str]:
        return {"ok": "mutated"}

    @app.get("/api/memberships/no-dep")
    async def no_dep() -> dict[str, str]:
        return {"oops": "missing security declaration"}

    @app.get("/api/auth/login", dependencies=[Depends(public_route)])
    async def login() -> dict[str, str]:
        return {"ok": "public"}

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"ok": "health"}

    @app.get("/api/legacy/anything")
    async def legacy() -> dict[str, str]:
        return {"ok": "legacy"}

    return app


@pytest_asyncio.fixture
async def seeded(db_session) -> AsyncIterator[dict[str, object]]:
    user = await identity_repo.insert_user(db_session, display_name="Owner")
    member_user = await identity_repo.insert_user(db_session, display_name="Member")
    org = await orgs_repo.insert_org(db_session, slug="acme")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member_user.id, org_id=org.id, role=Role.MEMBER, handle="mem"
    )

    raw_owner = "owner-raw-token"
    raw_member = "member-raw-token"
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw_owner),
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-owner",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw_member),
        user_id=member_user.id,
        workspace_id=None,
        csrf_token="csrf-member",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    yield {
        "org": org,
        "owner_user": user,
        "member_user": member_user,
        "owner_token": raw_owner,
        "member_token": raw_member,
    }


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_missing_x_org_slug_returns_400(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.get(
            "/api/memberships/ok",
            cookies={"yaaos_session": seeded["owner_token"]},
        )
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_org_slug"


@pytest.mark.asyncio
async def test_unknown_slug_returns_404(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.get(
            "/api/memberships/ok",
            cookies={"yaaos_session": seeded["owner_token"]},
            headers={"X-Org-Slug": "no-such-org"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "org_not_found"


@pytest.mark.asyncio
async def test_no_session_returns_401(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.get(
            "/api/memberships/ok",
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_role_returns_403(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.delete(
            "/api/memberships/admin-only",
            cookies={"yaaos_session": seeded["member_token"], "yaaos_csrf": "t"},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": "t"},
        )
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "insufficient_role"


@pytest.mark.asyncio
async def test_membership_success_returns_200(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.get(
            "/api/memberships/ok",
            cookies={"yaaos_session": seeded["owner_token"]},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": "yes"}


@pytest.mark.asyncio
async def test_route_without_security_dep_returns_500(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.get(
            "/api/memberships/no-dep",
            cookies={"yaaos_session": seeded["owner_token"]},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 500
    assert resp.json()["error"] == "route_missing_security_declaration"


@pytest.mark.asyncio
async def test_public_allowlist_bypasses_header_requirement() -> None:
    async with _client(_make_app()) as c:
        resp = await c.get("/api/auth/login")
        assert resp.status_code == 200
        resp = await c.get("/api/health")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_legacy_route_without_security_declaration_500s() -> None:
    """M02 default-deny: every `/api/*` route must declare security via
    `require()` or `public_route`. A route that returns 2xx without one
    gets swapped for 500 by the middleware's post-response guard."""
    async with _client(_make_app()) as c:
        resp = await c.get("/api/legacy/anything")
    assert resp.status_code == 500
    assert resp.json()["error"] == "route_missing_security_declaration"


def test_required_role_for_covers_every_action() -> None:
    """Action enum must stay in lockstep with the role registry."""
    from app.domain.sessions.dependencies import _REQUIRED_ROLE  # noqa: PLC0415

    missing = [a for a in Action if a not in _REQUIRED_ROLE]
    assert missing == [], f"Actions missing from _REQUIRED_ROLE: {missing}"


@pytest.mark.asyncio
async def test_csrf_mismatch_on_mutating_request_returns_403(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.post(
            "/api/memberships/mutate",
            cookies={
                "yaaos_session": seeded["owner_token"],
                "yaaos_csrf": "value-a",
            },
            headers={
                "X-Org-Slug": seeded["org"].slug,
                "X-CSRF-Token": "value-b",
            },
        )
    assert resp.status_code == 403
    assert resp.json()["error"] == "csrf_mismatch"


@pytest.mark.asyncio
async def test_csrf_match_on_mutating_request_passes(seeded) -> None:
    async with _client(_make_app()) as c:
        resp = await c.post(
            "/api/memberships/mutate",
            cookies={
                "yaaos_session": seeded["owner_token"],
                "yaaos_csrf": "same-token",
            },
            headers={
                "X-Org-Slug": seeded["org"].slug,
                "X-CSRF-Token": "same-token",
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_csrf_skipped_on_safe_method(seeded) -> None:
    """GET requests never participate in CSRF — no body, no state mutation."""
    async with _client(_make_app()) as c:
        resp = await c.get(
            "/api/memberships/ok",
            cookies={"yaaos_session": seeded["owner_token"]},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_role_check_does_not_leak_membership_existence(db_session, seeded) -> None:
    """Caller without membership in an existing org sees 404, not 403."""
    outsider = await identity_repo.insert_user(db_session, display_name="Outsider")
    raw = f"outsider-{uuid.uuid4()}"
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw),
        user_id=outsider.id,
        workspace_id=None,
        csrf_token="x",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    async with _client(_make_app()) as c:
        resp = await c.get(
            "/api/memberships/ok",
            cookies={"yaaos_session": raw},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "org_not_found"
