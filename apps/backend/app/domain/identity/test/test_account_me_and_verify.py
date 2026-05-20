"""Coverage for the M03 account surface: GET/PATCH /api/account/me + the
verify-only GitHub flow."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from app.core.auth import AuthMiddleware
from app.domain.auth import web as _auth_web  # noqa: F401
from app.domain.identity import account_web as _account_web  # noqa: F401
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.identity.models import OAuthIdentityRow
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role


def _app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    spec = _specs["account"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/account")
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
        db_session, user_id=user.id, org_id=org_a.id, role=Role.MEMBER, handle="alpha"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org_b.id, role=Role.MEMBER, handle="beta"
    )
    s = await session_lifecycle.create(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    yield {"user": user, "org_a": org_a, "org_b": org_b, "session": s}


# ── GET /api/account/me ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_account_me_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/account/me", headers={"X-Org-Slug": seeded["org_a"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_account_me_returns_orgs_and_handles(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/account/me",
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org_a"].slug},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] == "Acc"
    assert body["github_username"] is None
    handles = {o["slug"]: o["handle"] for o in body["orgs"]}
    assert handles == {"org-a": "alpha", "org-b": "beta"}


# ── PATCH /api/account/me ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_account_updates_display_name(seeded) -> None:
    sess = seeded["session"]
    async with _client() as c:
        r = await c.patch(
            "/api/account/me",
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
            "/api/account/me",
            json={"clear_github_username": True},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org_a"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json()["github_username"] is None


# ── Verify-only flow (provider stubbed) ──────────────────────────────────────


@pytest.fixture
def stub_github_provider(monkeypatch):
    """Swap in a stand-in provider under id=`github` for the duration of the
    test. Other providers (oauth_test) are not touched. Restores on teardown."""
    from app.domain.identity import providers as providers_module  # noqa: PLC0415
    from app.domain.identity.providers import ProviderProfile  # noqa: PLC0415

    class _StubGithubProvider:
        provider_id = "github"

        def authorization_url(self, *, state, redirect_uri):
            return f"https://stub.test/oauth?state={state}&redirect={redirect_uri}"

        async def exchange_code(self, *, code, redirect_uri):
            del code, redirect_uri
            return ProviderProfile(
                external_subject="42",
                primary_email="primary@x.test",
                email_verified=True,
                display_name="Octocat",
                mfa_satisfied=True,
                provider_login="octocat",
            )

    prior = providers_module._REGISTRY.get("github")
    providers_module._REGISTRY["github"] = _StubGithubProvider()
    try:
        yield
    finally:
        if prior is not None:
            providers_module._REGISTRY["github"] = prior
        else:
            providers_module._REGISTRY.pop("github", None)


@pytest.mark.asyncio
async def test_verify_start_redirects_to_provider(seeded, stub_github_provider) -> None:
    del stub_github_provider
    async with _client() as c:
        r = await c.get(
            "/api/account/github/verify",
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org_a"].slug},
            follow_redirects=False,
        )
    assert r.status_code == 303, r.text
    assert "stub.test/oauth" in r.headers["location"]


@pytest.mark.asyncio
async def test_verify_callback_writes_username_no_identity_row(
    seeded, stub_github_provider, db_session
) -> None:
    del stub_github_provider
    # Generate a valid state with the right user_id by hitting /verify first.
    async with _client() as c:
        start = await c.get(
            "/api/account/github/verify",
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org_a"].slug},
            follow_redirects=False,
        )
        # Extract state from the redirect URL.
        from urllib.parse import parse_qs, urlparse  # noqa: PLC0415

        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        r = await c.get(
            "/api/account/github/verify/callback",
            params={"code": "stub-code", "state": state},
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org_a"].slug},
        )
    assert r.status_code == 200, r.text
    assert r.json()["github_username"] == "octocat"

    # No oauth_identities row written for the verify flow.
    rows = (
        (
            await db_session.execute(
                select(OAuthIdentityRow).where(OAuthIdentityRow.user_id == seeded["user"].id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_verify_callback_rejects_state_for_other_user(seeded, stub_github_provider, db_session) -> None:
    del stub_github_provider
    # Mint a state for a different user via the serializer directly.
    from uuid import uuid4  # noqa: PLC0415

    from app.domain.identity.account_web import _verify_state_serializer  # noqa: PLC0415

    bogus_state = _verify_state_serializer().dumps({"user_id": str(uuid4())})
    async with _client() as c:
        r = await c.get(
            "/api/account/github/verify/callback",
            params={"code": "x", "state": bogus_state},
            cookies={"yaaos_session": seeded["session"].raw_token},
            headers={"X-Org-Slug": seeded["org_a"].slug},
        )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "state_user_mismatch"


# ── PATCH /api/memberships/me/{org_id} ──────────────────────────────────────


def _memberships_app() -> FastAPI:
    from app.core.webserver.registry import _specs  # noqa: PLC0415
    from app.domain.orgs import web as _orgs_web  # noqa: F401, PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    spec = _specs["memberships"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/memberships")
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
        role=Role.MEMBER,
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
