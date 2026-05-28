"""Coverage for GET /api/auth/sso/discover (+ audit follow-up).

The endpoint is `public_route` — no session required (the Login page
calls it before any cookie is set). Returns `{provider: "github"}` when
no org claims the email's domain; returns `{provider: "saml",
saml_org_slug}` when an enabled `sso_configs` row claims the domain.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import AuthMiddleware
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import upsert_config
from app.domain.sessions import web as _sessions_web  # noqa: F401


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"sessions"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.mark.asyncio
async def test_discover_returns_github_when_no_org_claims_domain() -> None:
    async with _client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": "alice@example.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "github"


@pytest.mark.asyncio
async def test_discover_rejects_empty_email() -> None:
    async with _client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": ""})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_discover_rejects_email_without_at_sign() -> None:
    async with _client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": "notanemail"})
    assert r.status_code == 422


@pytest_asyncio.fixture
async def claimed_org(db_session):
    org = await orgs_repo.insert_org(db_session, slug="acme", display_name="Acme")
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<EntityDescriptor/>",
        jit_enabled=False,
        enabled=True,
        exempt_owner_user_id=None,
        email_domains=["acme.com"],
    )
    await db_session.commit()
    yield org


@pytest.mark.service
@pytest.mark.asyncio
async def test_discover_returns_saml_for_claimed_domain(claimed_org) -> None:
    async with _client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": "anyone@acme.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "saml"
    assert body["saml_org_slug"] == claimed_org.slug


@pytest.mark.service
@pytest.mark.asyncio
async def test_discover_is_case_insensitive_on_domain(claimed_org) -> None:
    async with _client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": "Bob@ACME.COM"})
    body = r.json()
    assert body["provider"] == "saml"


@pytest.mark.service
@pytest.mark.asyncio
async def test_discover_skips_disabled_configs(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="off", display_name="Off")
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<EntityDescriptor/>",
        jit_enabled=False,
        enabled=False,  # disabled — must not route logins.
        exempt_owner_user_id=None,
        email_domains=["off.example"],
    )
    await db_session.commit()
    async with _client() as c:
        r = await c.get("/api/auth/sso/discover", params={"email": "x@off.example"})
    assert r.json()["provider"] == "github"
