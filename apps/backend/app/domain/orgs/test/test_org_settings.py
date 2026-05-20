"""Coverage for PATCH /api/orgs (session_timeout_override) + the idle-timeout
check the require() dep performs based on the org's override."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.core.auth import AuthMiddleware
from app.domain.auth import web as _auth_web  # noqa: F401
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.identity.models import SessionRow
from app.domain.orgs import org_settings_web as _org_settings_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import web as _orgs_web  # noqa: F401
from app.domain.orgs.models import OrgRow
from app.domain.orgs.types import Role


def _patch_app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    spec = _specs["orgs"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/orgs")
    return app


def _patch_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_patch_app()), base_url="http://test")


def _idle_probe_app() -> FastAPI:
    """An app with a single MEMBERS_READ-gated endpoint so we can prove the
    idle-timeout check inside `require()` rejects an old session."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    from app.core.webserver.registry import _specs  # noqa: PLC0415

    # Reuse the memberships router so we have an org-scoped GET to hit.
    spec = _specs["memberships"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/memberships")
    return app


def _idle_probe_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_idle_probe_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    owner = await identity_repo.insert_user(db_session, display_name="O")
    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="ts-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.id, role=Role.OWNER, handle="own"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.id, role=Role.MEMBER, handle="mem"
    )
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "admin_sess": admin_sess,
        "member_sess": member_sess,
    }


# ── PATCH /api/orgs ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_org_unauthenticated_401(seeded) -> None:
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 30},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": "x"},
            cookies={"yaaos_csrf": "x"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_patch_org_member_forbidden(seeded) -> None:
    sess = seeded["member_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 30},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_patch_org_admin_can_set_override(seeded, db_session) -> None:
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 30},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"slug": seeded["org"].slug, "session_timeout_override": 30}

    org_row = (await db_session.execute(select(OrgRow).where(OrgRow.id == seeded["org"].id))).scalar_one()
    assert org_row.session_timeout_override == 30


@pytest.mark.asyncio
async def test_patch_org_admin_can_clear_override(seeded, db_session) -> None:
    # Pre-set, then clear.
    org_row = (await db_session.execute(select(OrgRow).where(OrgRow.id == seeded["org"].id))).scalar_one()
    org_row.session_timeout_override = 30
    await db_session.commit()

    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": None},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["session_timeout_override"] is None


@pytest.mark.asyncio
async def test_patch_org_rejects_non_positive(seeded) -> None:
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"session_timeout_override": 0},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_org_ignores_unrelated_keys(seeded, db_session) -> None:
    """Keys we don't recognise are silently ignored — the body schema is
    open-ended so future M03 settings can be added without breaking older
    clients."""
    sess = seeded["admin_sess"]
    async with _patch_client() as c:
        r = await c.patch(
            "/api/orgs",
            json={"future_field": "ignored", "session_timeout_override": 45},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["session_timeout_override"] == 45


# ── Idle timeout (per-org override) ──────────────────────────────────────────


async def _backdate_session_last_seen(db_session, *, token_hash: str, minutes_ago: int) -> None:
    """Test helper: rewrite a session row's `last_seen_at` to simulate idleness."""
    row = (
        await db_session.execute(select(SessionRow).where(SessionRow.token_hash == token_hash))
    ).scalar_one()
    row.last_seen_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    await db_session.commit()


@pytest.mark.asyncio
async def test_idle_session_rejected_when_override_set(seeded, db_session) -> None:
    """Admin pins the override to 10 minutes; a session last seen 30 minutes
    ago is rejected by the require() dep with 401 session_idle_expired."""
    org_row = (await db_session.execute(select(OrgRow).where(OrgRow.id == seeded["org"].id))).scalar_one()
    org_row.session_timeout_override = 10
    sess = seeded["admin_sess"]
    await _backdate_session_last_seen(
        db_session, token_hash=identity_repo.hash_token(sess.raw_token), minutes_ago=30
    )

    async with _idle_probe_client() as c:
        r = await c.get(
            "/api/memberships",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["error"] == "session_idle_expired"


@pytest.mark.asyncio
async def test_idle_session_within_override_passes(seeded, db_session) -> None:
    """Within the override window: passes."""
    org_row = (await db_session.execute(select(OrgRow).where(OrgRow.id == seeded["org"].id))).scalar_one()
    org_row.session_timeout_override = 60
    sess = seeded["admin_sess"]
    await _backdate_session_last_seen(
        db_session, token_hash=identity_repo.hash_token(sess.raw_token), minutes_ago=30
    )

    async with _idle_probe_client() as c:
        r = await c.get(
            "/api/memberships",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_idle_default_used_when_override_null(seeded, db_session) -> None:
    """No override → global SESSION_IDLE_TIMEOUT (12h) governs. A 30-minute
    idle session is still fresh under the default."""
    sess = seeded["admin_sess"]
    await _backdate_session_last_seen(
        db_session, token_hash=identity_repo.hash_token(sess.raw_token), minutes_ago=30
    )
    async with _idle_probe_client() as c:
        r = await c.get(
            "/api/memberships",
            cookies={"yaaos_session": sess.raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
