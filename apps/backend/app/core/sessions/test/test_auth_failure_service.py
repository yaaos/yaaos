"""Service tests for AuthFailure — cookie clearing + body shape + audit row.

Three failure cases must all clear `yaaos_session` + `yaaos_csrf` so the
SPA's central 401 handler can redirect to /login without the browser
re-presenting the dead cookie on the next request. Idle timeout
additionally writes an `entity=user / action=logout / payload.kind=idle_timeout`
audit row so operators have a server-side answer to "why did my session
die" — matching the existing hard-expiry audit pattern in
`apps/backend/app/core/identity/scheduler.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI

from app.core.audit_log import list_for_entity
from app.core.auth import Action, AuthMiddleware, Role, register_handler
from app.core.identity import repository as identity_repo
from app.core.sessions import require
from app.core.sessions import web as _auth_web  # noqa: F401  -- registers /api/auth/me
from app.domain.orgs import repository as orgs_repo
from app.testing.seed import set_session_last_seen as _set_session_last_seen_for_tests


def _make_app() -> FastAPI:
    """Test app mirrors test_middleware.py's pattern + registers the
    AuthFailure handler (this is what create_app does in prod). Mounts
    `sessions` so /api/auth/me is reachable, and exposes one org-scoped
    route that goes through `require()` so we can hit the idle-timeout +
    no-session paths."""
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    register_handler(app)
    mount_specs(app, only={"sessions"})

    @app.get(
        "/api/memberships/ok",
        dependencies=[Depends(require(Action.MEMBERS_READ))],
    )
    async def ok() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_make_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session) -> AsyncIterator[dict[str, object]]:
    """Owner with a valid session + a writable org. Tests that need a
    stale `last_seen_at` mutate the row in-place."""
    user = await identity_repo.insert_user(db_session, display_name="Owner")
    org = await orgs_repo.insert_org(db_session, slug=f"af-{uuid.uuid4().hex[:8]}")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.OWNER, handle="own"
    )

    raw_token = f"af-owner-{uuid.uuid4().hex[:8]}"
    await identity_repo.insert_session(
        db_session,
        token_hash=identity_repo.hash_token(raw_token),
        user_id=user.id,
        workspace_id=None,
        csrf_token="csrf-af",
        ip=None,
        user_agent=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await db_session.commit()
    yield {"org": org, "user": user, "token": raw_token}


def _assert_cookies_cleared(resp: httpx.Response) -> None:
    """Both yaaos_session and yaaos_csrf must come back with Max-Age=0.
    httpx exposes the raw Set-Cookie header values via resp.headers (case
    -insensitive); we accept multiple Set-Cookie via headers.get_list."""
    raw = (
        resp.headers.get_list("set-cookie")
        if hasattr(resp.headers, "get_list")
        else [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]
    )
    session_cleared = any("yaaos_session=" in h and "Max-Age=0" in h for h in raw)
    csrf_cleared = any("yaaos_csrf=" in h and "Max-Age=0" in h for h in raw)
    assert session_cleared, f"yaaos_session not cleared; Set-Cookie headers: {raw}"
    assert csrf_cleared, f"yaaos_csrf not cleared; Set-Cookie headers: {raw}"


@pytest.mark.asyncio
async def test_me_without_session_clears_cookies() -> None:
    """`/api/auth/me` with no session cookie → 401 + Set-Cookie clears
    both cookies. The SPA's central 401 handler reads the body's error
    code to pick the banner."""
    async with _client() as c:
        resp = await c.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthenticated"}
    _assert_cookies_cleared(resp)


@pytest.mark.asyncio
async def test_org_scoped_no_session_clears_cookies(seeded) -> None:
    """Org-scoped endpoint with X-Org-Slug but no session cookie → 401
    `unauthenticated` + cleared cookies. This is the "cold deeplink
    while logged out" path — central SPA handler will then redirect to
    /login?reason=signed_out."""
    async with _client() as c:
        resp = await c.get(
            "/api/memberships/ok",
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert resp.status_code == 401
    # Handler returns {"error": "unauthenticated"} directly — no FastAPI
    # `detail` wrapping, so the SPA reads `body.error` regardless of
    # whether the 401 came from a raise (deps) or a return (web.py).
    assert resp.json() == {"error": "unauthenticated"}
    _assert_cookies_cleared(resp)


@pytest.mark.asyncio
async def test_org_scoped_idle_timeout_clears_cookies_and_writes_audit(seeded, db_session) -> None:
    """Stale `last_seen_at` past the org's idle window → 401
    `session_idle_expired` + cleared cookies + one
    `user/logout/idle_timeout` audit row tagged to this org. Gives
    operators a server-side trail of why the session died (matches the
    existing hard-expiry audit in scheduler._purge_expired_sessions)."""
    # Force the session's last_seen_at far enough in the past that the
    # default idle window has elapsed. SESSION_IDLE_TIMEOUT default is
    # measured in minutes; 1 day back is comfortably stale.
    token_hash = identity_repo.hash_token(seeded["token"])
    await _set_session_last_seen_for_tests(
        db_session,
        token_hash=token_hash,
        last_seen_at=datetime.now(UTC) - timedelta(days=1),
    )
    await db_session.commit()

    async with _client() as c:
        resp = await c.get(
            "/api/memberships/ok",
            cookies={"yaaos_session": seeded["token"]},
            headers={"X-Org-Slug": seeded["org"].slug},
        )

    assert resp.status_code == 401
    assert resp.json() == {"error": "session_idle_expired"}
    _assert_cookies_cleared(resp)

    # Audit row must be present.
    audit_rows = await list_for_entity(
        "user", seeded["user"].id, org_id=seeded["org"].org_id, kinds=["logout"]
    )
    assert len(audit_rows) == 1, f"expected 1 idle-timeout audit row, got {len(audit_rows)}"
    assert audit_rows[0].payload == {"kind": "idle_timeout"}
    assert audit_rows[0].entity_id == seeded["user"].id
