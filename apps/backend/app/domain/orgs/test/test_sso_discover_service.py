"""Service test: GET /api/sso/discover is served by domain/orgs.

The discover endpoint moved from `/api/auth/sso/discover` (core/sessions)
to `/api/sso/discover` (domain/orgs/sso_web). This test verifies:
- The endpoint lives at the new path and behaves identically.
- core/sessions no longer registers a `/api/auth/sso/discover` route.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.domain.orgs import insert_org, upsert_config

# sso_web is loaded by domain.orgs.__init__ — no explicit import needed


def _sso_app() -> FastAPI:
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"sso"})
    return app


def _sso_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_sso_app()), base_url="http://test")


def _sessions_app() -> FastAPI:
    """Auth-only app — verify /api/auth/sso/discover is gone."""
    import app.core.sessions  # noqa: PLC0415  -- triggers auth route registration
    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"sessions"})
    return app


def _sessions_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_sessions_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_sso_discover_served_by_orgs_returns_github_for_unknown_domain() -> None:
    """No SSO config → provider=github, served at /api/sso/discover."""
    async with _sso_client() as c:
        r = await c.get("/api/sso/discover", params={"email": "user@unknown.example"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "github"


@pytest.mark.asyncio
async def test_sso_discover_rejects_invalid_email() -> None:
    async with _sso_client() as c:
        r = await c.get("/api/sso/discover", params={"email": "notanemail"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_sso_discover_no_longer_on_auth_prefix() -> None:
    """`/api/auth/sso/discover` must 404 — the route was removed from core/sessions."""
    async with _sessions_client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": "user@example.com"})
    assert r.status_code == 404, f"Expected 404 (route removed), got {r.status_code}: {r.text}"


@pytest_asyncio.fixture
async def claimed_org(db_session):
    org = await insert_org(db_session, slug="discover-acme", display_name="Acme")
    await upsert_config(
        db_session,
        org_id=org.org_id,
        idp_metadata_xml="<EntityDescriptor/>",
        jit_enabled=False,
        enabled=True,
        exempt_owner_user_id=None,
        email_domains=["discover-acme.com"],
    )
    await db_session.commit()
    yield org


@pytest.mark.asyncio
async def test_sso_discover_served_by_orgs(claimed_org) -> None:
    """Enabled SSO config claiming `discover-acme.com` → provider=saml with slug."""
    async with _sso_client() as c:
        r = await c.get("/api/sso/discover", params={"email": "anyone@discover-acme.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "saml"
    assert body["saml_org_slug"] == claimed_org.slug


@pytest.mark.asyncio
async def test_sso_discover_skips_disabled_config(db_session) -> None:
    org = await insert_org(db_session, slug="discover-off", display_name="Off")
    await upsert_config(
        db_session,
        org_id=org.org_id,
        idp_metadata_xml="<EntityDescriptor/>",
        jit_enabled=False,
        enabled=False,
        exempt_owner_user_id=None,
        email_domains=["discover-off.example"],
    )
    await db_session.commit()
    async with _sso_client() as c:
        r = await c.get("/api/sso/discover", params={"email": "x@discover-off.example"})
    assert r.json()["provider"] == "github"
