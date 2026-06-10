"""Coverage for the user-scoped + readiness endpoints on `/api/orgs`.

- `GET /api/orgs/mine` — cross-org list for the cookie-bearer.
- `GET /api/orgs/config-status` — per-org readiness for the "not configured" gate.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware, Role
from app.core.identity import repository as identity_repo
from app.core.identity import sessions as session_lifecycle
from app.core.sessions import web as _sessions_web  # noqa: F401
from app.domain.orgs import org_settings_web as _org_settings_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import web as _orgs_web  # noqa: F401
from app.domain.orgs.onboarding import (
    _reset_contributors_for_tests,
    register_onboarding_contributor,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"orgs", "memberships", "sessions"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="U")
    org_a = await orgs_repo.insert_org(db_session, slug="alpha", display_name="Alpha")
    org_b = await orgs_repo.insert_org(db_session, slug="beta", display_name="Beta")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_a.org_id, role=Role.OWNER, handle="u-a"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_b.org_id, role=Role.BUILDER, handle="u-b"
    )
    sess = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"user": user, "org_a": org_a, "org_b": org_b, "sess": sess}


# ── GET /api/orgs/mine ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mine_unauthenticated_returns_401() -> None:
    async with _client() as c:
        r = await c.get("/api/orgs/mine")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_mine_returns_user_memberships_sorted_by_slug(seeded) -> None:
    sess = seeded["sess"]
    async with _client() as c:
        r = await c.get("/api/orgs/mine", cookies={"yaaos_session": sess.raw_token})
    assert r.status_code == 200
    body = r.json()
    assert [o["slug"] for o in body] == ["alpha", "beta"]
    assert body[0]["role"] == "owner"
    assert body[1]["role"] == "builder"
    # last_used_at is null — no per-membership column today.
    assert body[0]["last_used_at"] is None


# ── GET /api/orgs/config-status ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_status_unconfigured_reports_missing_pieces(seeded) -> None:
    _reset_contributors_for_tests()
    # Both contributors absent → both come back "missing"; workspace
    # not configured (no registered_iam_arn) → also missing.
    sess = seeded["sess"]
    async with _client() as c:
        r = await c.get(
            "/api/orgs/config-status",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org_a"].slug},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert set(body["missing"]) == {"vcs", "api_key", "workspace"}
    assert any(a["user_id"] == str(seeded["user"].id) for a in body["admins"])


@pytest.mark.asyncio
async def test_config_status_fully_configured(seeded, db_session) -> None:
    _reset_contributors_for_tests()

    async def yes(_org_id):
        return True

    register_onboarding_contributor("github_app_installed", yes)
    register_onboarding_contributor("anthropic_key_set", yes)
    from app.core.tenancy import update_org_fields  # noqa: PLC0415

    # Workspace is configured when registered_iam_arn is set.
    await update_org_fields(
        db_session,
        seeded["org_a"].org_id,
        registered_iam_arn="arn:aws:iam::123456789012:role/yaaos-agent",
        aws_region="us-east-1",
    )
    await db_session.commit()

    sess = seeded["sess"]
    async with _client() as c:
        r = await c.get(
            "/api/orgs/config-status",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Yaaos-Org-Slug": seeded["org_a"].slug},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["missing"] == []
