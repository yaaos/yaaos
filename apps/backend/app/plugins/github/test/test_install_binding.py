"""GitHub App install ↔ org binding tests.

Covers state signature verification, mismatched-state rejection, and the
post-install callback writing a complete `github_app_installations` row.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.core.auth import AuthMiddleware
from app.plugins.github.models import GitHubAppInstallationRow
from app.plugins.github.web import _install_state_serializer


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"github"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seed_org(db_session):
    org_id = uuid4()
    yield org_id


@pytest.mark.asyncio
async def test_install_callback_bad_state_returns_400(seed_org) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/github/install_callback",
            params={"state": "not-a-signed-value", "installation_id": "42"},
            follow_redirects=False,
        )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "state_invalid"


@pytest.mark.asyncio
async def test_install_callback_missing_params_returns_400() -> None:
    async with _client() as c:
        r = await c.get("/api/github/install_callback", follow_redirects=False)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_install_callback_happy_path_writes_app_install_row(seed_org, db_session, monkeypatch) -> None:
    # Stub the GitHub API call so the callback runs without credentials or
    # network. The row's `account_login` should reflect the stubbed value.
    async def _stub_fetch(installation_id: int):
        return "acme-org"

    monkeypatch.setattr(
        "app.plugins.github.web.fetch_install_account_login",
        _stub_fetch,
    )

    state = _install_state_serializer().dumps({"org_id": str(seed_org)})
    async with _client() as c:
        r = await c.get(
            "/api/github/install_callback",
            params={"state": state, "installation_id": "9999"},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303, 307)

    # Use the override-aware session() so the test sees the route's commit.
    from app.core.database import session as db_session_factory  # noqa: PLC0415

    async with db_session_factory() as s:
        row = (
            await s.execute(
                select(GitHubAppInstallationRow).where(GitHubAppInstallationRow.install_external_id == "9999")
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.org_id == seed_org
        assert row.account_login == "acme-org"
        assert row.status == "active"
        # Cleanup.
        await s.delete(row)
        await s.commit()


@pytest_asyncio.fixture
async def seeded_owner(db_session):
    """Owner + Admin sessions on a fresh org. Builds the auth surface the
    `/install/start` route needs to exercise role gating (Owner-only)."""
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import repository as identity_repo  # noqa: PLC0415
    from app.core.identity import sessions as session_lifecycle  # noqa: PLC0415
    from app.domain.orgs import repository as orgs_repo  # noqa: PLC0415

    owner = await identity_repo.insert_user(db_session, display_name="O")
    admin = await identity_repo.insert_user(db_session, display_name="A")
    org = await orgs_repo.insert_org(db_session, slug="gh-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="ownr"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.org_id, role=Role.ADMIN, handle="admin"
    )
    owner_sess = await session_lifecycle.create(db_session, user_id=owner.id, workspace_id=None)
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    await db_session.commit()
    yield {"org": org, "owner_sess": owner_sess, "admin_sess": admin_sess}


@pytest.mark.asyncio
async def test_install_start_returns_signed_redirect_url(seeded_owner, monkeypatch) -> None:
    """`POST /api/github/install/start` returns a github.com URL with a signed
    `state` query param the callback can verify. Without this route the
    SPA's install button can't trigger a state-signed handshake."""
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos-test")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    sess = seeded_owner["owner_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/github/install/start",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Yaaos-Org-Slug": seeded_owner["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "/apps/yaaos-test/installations/new?state=" in body["redirect_url"]
    state = body["redirect_url"].split("?state=", 1)[1]
    payload = _install_state_serializer().loads(state, max_age=900)
    assert payload == {"org_id": str(seeded_owner["org"].org_id)}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_install_start_admin_forbidden(seeded_owner, monkeypatch) -> None:
    """`GITHUB_APP_LINK` is Owner-only — Admin sessions get 403."""
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "yaaos-test")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    sess = seeded_owner["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/github/install/start",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Yaaos-Org-Slug": seeded_owner["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 403
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_install_start_409_when_no_app_provisioned(seeded_owner, monkeypatch) -> None:
    monkeypatch.setenv("YAAOS_GITHUB_APP_SLUG", "")
    from app.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    sess = seeded_owner["owner_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/github/install/start",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Yaaos-Org-Slug": seeded_owner["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "app_not_provisioned"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_state_signature_is_per_secret(seed_org) -> None:
    # A token signed with a different secret/salt must NOT verify.
    from itsdangerous import URLSafeTimedSerializer  # noqa: PLC0415

    forged = URLSafeTimedSerializer("wrong-secret", salt="yaaos-github-install").dumps(
        {"org_id": str(seed_org)}
    )
    async with _client() as c:
        r = await c.get(
            "/api/github/install_callback",
            params={"state": forged, "installation_id": "1"},
            follow_redirects=False,
        )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "state_invalid"


# Keep UUID import in use (linter).
_ = UUID
