"""SSO config + ACS + middleware-enforcement tests."""

from __future__ import annotations

import httpx
import pyotp
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.core.auth import Action, AuthMiddleware
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.identity import totp as totp_lifecycle
from app.domain.orgs import audit_web as _audit_web  # noqa: F401
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs import sso_web as _sso_web  # noqa: F401
from app.domain.orgs import upsert_config
from app.domain.orgs.types import Role
from app.domain.sessions import require
from app.plugins.saml_test import sign_assertion


def _app() -> FastAPI:
    from fastapi import Depends  # noqa: PLC0415

    from app.core.webserver import mount_specs  # noqa: PLC0415

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"sso"})

    # A protected endpoint under /api/memberships so we can exercise the
    # SSO-enforcement branch in `require()`.
    @app.get("/api/memberships/probe", dependencies=[Depends(require(Action.MEMBERS_READ))])
    async def probe() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def sso_org(db_session):
    user = await identity_repo.insert_user(db_session, display_name="SSO User")
    await identity_repo.add_email(db_session, user_id=user.id, email="ssouser@example.com", verified=True)
    org = await orgs_repo.insert_org(db_session, slug="sso-org")
    await orgs_repo.insert_membership(
        db_session, user_id=user.id, org_id=org.id, role=Role.BUILDER, handle="sso"
    )
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<EntityDescriptor>fake</EntityDescriptor>",
        jit_enabled=False,
        enabled=True,
    )
    await db_session.commit()
    yield {"user": user, "org": org}


@pytest.mark.asyncio
async def test_metadata_endpoint_returns_xml(sso_org) -> None:
    async with _client() as c:
        r = await c.get("/api/sso/sso-org/metadata")
    assert r.status_code == 200
    assert "EntityDescriptor" in r.text
    assert "AssertionConsumerService" in r.text


@pytest.mark.asyncio
async def test_acs_with_invalid_assertion_returns_400(sso_org) -> None:
    async with _client() as c:
        r = await c.post(
            "/api/sso/sso-org/acs",
            json={"SAMLResponse": "not-a-signed-token"},
            follow_redirects=False,
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_acs_happy_path_marks_session_sso_satisfied(sso_org, db_session) -> None:
    token = sign_assertion({"email": "ssouser@example.com", "name_id": "ssouser"})
    s = await session_lifecycle.create(db_session, user_id=sso_org["user"].id, workspace_id=None)
    await db_session.commit()
    async with _client() as c:
        r = await c.post(
            "/api/sso/sso-org/acs",
            json={"SAMLResponse": token},
            cookies={"yaaos_session": s.raw_token},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303), r.text


@pytest.mark.asyncio
async def test_acs_jit_creates_user_when_enabled(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="jit-org")
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<EntityDescriptor/>",
        jit_enabled=True,
        enabled=True,
    )
    await db_session.commit()

    token = sign_assertion({"email": "newjit@example.com", "name_id": "newjit"})
    async with _client() as c:
        r = await c.post(
            "/api/sso/jit-org/acs",
            json={"SAMLResponse": token},
            follow_redirects=False,
        )
    assert r.status_code in (302, 303)

    from app.core.database import session as factory  # noqa: PLC0415

    async with factory() as s:
        u = await identity_repo.find_user_by_email(s, "newjit@example.com")
        assert u is not None


@pytest.mark.asyncio
async def test_acs_no_jit_rejects_unknown_user(db_session) -> None:
    org = await orgs_repo.insert_org(db_session, slug="nojit-org")
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<EntityDescriptor/>",
        jit_enabled=False,
        enabled=True,
    )
    await db_session.commit()
    token = sign_assertion({"email": "nobody@example.com", "name_id": "nobody"})
    async with _client() as c:
        r = await c.post("/api/sso/nojit-org/acs", json={"SAMLResponse": token}, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_middleware_blocks_without_sso_satisfaction(sso_org, db_session) -> None:
    s = await session_lifecycle.create(db_session, user_id=sso_org["user"].id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        r = await c.get(
            "/api/memberships/probe",
            cookies={"yaaos_session": s.raw_token},
            headers={"X-Org-Slug": "sso-org"},
        )
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "sso_required"


@pytest.mark.asyncio
async def test_middleware_allows_when_sso_satisfied(sso_org, db_session) -> None:
    s = await session_lifecycle.create(db_session, user_id=sso_org["user"].id, workspace_id=None)
    await session_lifecycle.mark_sso_satisfied(db_session, s.raw_token, org_id=sso_org["org"].id)
    await db_session.commit()

    async with _client() as c:
        r = await c.get(
            "/api/memberships/probe",
            cookies={"yaaos_session": s.raw_token},
            headers={"X-Org-Slug": "sso-org"},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_exempt_owner_bypasses_sso_when_totp_verified(db_session) -> None:
    owner = await identity_repo.insert_user(db_session, display_name="Owner")
    await identity_repo.add_email(db_session, user_id=owner.id, email="owner@example.com", verified=True)
    org = await orgs_repo.insert_org(db_session, slug="exempt-org")
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.id, role=Role.OWNER, handle="ow"
    )
    seed, _ = await totp_lifecycle.enroll(db_session, user_id=owner.id)
    await totp_lifecycle.verify(db_session, user_id=owner.id, code=pyotp.TOTP(seed).now())
    await upsert_config(
        db_session,
        org_id=org.id,
        idp_metadata_xml="<EntityDescriptor/>",
        enabled=True,
        exempt_owner_user_id=owner.id,
    )
    s = await session_lifecycle.create(db_session, user_id=owner.id, workspace_id=None)
    await db_session.commit()

    async with _client() as c:
        r = await c.get(
            "/api/memberships/probe",
            cookies={"yaaos_session": s.raw_token},
            headers={"X-Org-Slug": "exempt-org"},
        )
    assert r.status_code == 200
