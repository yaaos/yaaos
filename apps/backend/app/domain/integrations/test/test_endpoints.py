"""HTTP coverage for /api/mcp-proxy — list, connect, validate, patch, delete.

The endpoint tests use a stub IntegrationProvider so the OAuth + validate
paths don't touch the network. The dedicated connect_callback round-trip
test verifies the signed-state flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from itsdangerous import URLSafeTimedSerializer
from pydantic import SecretStr
from sqlalchemy import select

from app.core.audit_log import list_for_org
from app.core.auth import AuthMiddleware
from app.core.config import get_settings
from app.core.oauth import ProviderConfig, Tokens
from app.domain.identity import repository as identity_repo
from app.domain.identity import sessions as session_lifecycle
from app.domain.integrations import web as _integ_web  # noqa: F401
from app.domain.integrations.types import _REGISTRY
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.types import Role
from app.domain.sessions import web as _auth_web  # noqa: F401


def _make_stub_config() -> ProviderConfig:
    return ProviderConfig(
        authorize_url="https://stub.test/authorize",
        token_url="https://stub.test/token",
        refresh_url="https://stub.test/token",
        mcp_url="https://stub.test/mcp",
        client_id="cid",
        client_secret=SecretStr("csecret"),
        scope_separator=" ",
        default_scopes=("read",),
        known_read_tools=("get_thing",),
        known_write_tools=("update_thing",),
    )


@dataclass
class _StubProvider:
    provider_id: str = "stub"
    config: ProviderConfig = field(default_factory=_make_stub_config)
    validate_returns: bool = True

    async def validate(self, access_token: SecretStr) -> bool:
        del access_token
        return self.validate_returns


@pytest.fixture
def stub_provider():
    prior = _REGISTRY.get("stub")
    provider = _StubProvider()
    _REGISTRY["stub"] = provider
    try:
        yield provider
    finally:
        if prior is not None:
            _REGISTRY["stub"] = prior
        else:
            _REGISTRY.pop("stub", None)


@pytest.fixture
def stub_exchange(monkeypatch):
    async def fake_exchange(config, *, code, redirect_uri):
        del config, code, redirect_uri
        return Tokens(
            access_token=SecretStr("access-1"),
            refresh_token=SecretStr("refresh-1"),
            expires_in=3600,
            scope="read",
            raw={},
        )

    monkeypatch.setattr("app.domain.integrations.service.exchange_code", fake_exchange)


def _app() -> FastAPI:

    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"integrations"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest_asyncio.fixture
async def seeded(db_session):
    admin = await identity_repo.insert_user(db_session, display_name="A")
    member = await identity_repo.insert_user(db_session, display_name="M")
    org = await orgs_repo.insert_org(db_session, slug="integ-ep")
    await orgs_repo.insert_membership(
        db_session, user_id=admin.id, org_id=org.id, role=Role.ADMIN, handle="adm"
    )
    await orgs_repo.insert_membership(
        db_session, user_id=member.id, org_id=org.id, role=Role.BUILDER, handle="mem"
    )
    admin_sess = await session_lifecycle.create(db_session, user_id=admin.id, workspace_id=None)
    member_sess = await session_lifecycle.create(db_session, user_id=member.id, workspace_id=None)
    await db_session.commit()
    yield {
        "org": org,
        "admin": admin,
        "admin_sess": admin_sess,
        "member_sess": member_sess,
    }


@pytest.mark.asyncio
async def test_list_unauthenticated_401(seeded) -> None:
    async with _client() as c:
        r = await c.get("/api/mcp-proxy", headers={"X-Org-Slug": seeded["org"].slug})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_member_forbidden(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/mcp-proxy",
            cookies={"yaaos_session": seeded["member_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_list_admin_sees_registered_providers_with_not_set(seeded, stub_provider) -> None:
    del stub_provider
    async with _client() as c:
        r = await c.get(
            "/api/mcp-proxy",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
        )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(p["provider"] == "stub" and p["status"] == "not_set" for p in rows)


@pytest.mark.asyncio
async def test_connect_start_redirects_to_provider_with_signed_state(seeded, stub_provider) -> None:
    del stub_provider
    async with _client() as c:
        r = await c.get(
            "/api/mcp-proxy/stub/connect",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert "stub.test/authorize" in r.headers["location"]
    assert "state=" in r.headers["location"]
    assert "client_id=cid" in r.headers["location"]


@pytest.mark.asyncio
async def test_connect_start_unknown_provider_404(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/mcp-proxy/ghost/connect",
            cookies={"yaaos_session": seeded["admin_sess"].raw_token},
            headers={"X-Org-Slug": seeded["org"].slug},
            follow_redirects=False,
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_callback_round_trip_persists_credential(
    seeded, stub_provider, stub_exchange, db_session
) -> None:
    del stub_provider, stub_exchange
    # Mint a valid state via the same serializer the endpoint uses.
    state = URLSafeTimedSerializer(
        get_settings().yaaos_invitation_token_secret.get_secret_value(), salt="yaaos-integration-connect"
    ).dumps(
        {
            "org_id": str(seeded["org"].id),
            "user_initiating": str(seeded["admin"].id),
            "provider": "stub",
        }
    )
    async with _client() as c:
        r = await c.get(
            "/api/mcp-proxy/stub/callback",
            params={"code": "abc", "state": state},
            follow_redirects=False,
        )
    assert r.status_code == 303
    # Row was persisted; encrypted-at-rest tokens stored.
    from app.domain.integrations.models import McpCredentialRow  # noqa: PLC0415

    rows = (
        (
            await db_session.execute(
                select(McpCredentialRow).where(McpCredentialRow.org_id == seeded["org"].id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].provider == "stub"
    assert rows[0].encrypted_access_token != "access-1"


@pytest.mark.asyncio
async def test_callback_rejects_tampered_state(seeded) -> None:
    async with _client() as c:
        r = await c.get(
            "/api/mcp-proxy/stub/callback",
            params={"code": "abc", "state": "tampered.state.value"},
            follow_redirects=False,
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_callback_rejects_wrong_provider_in_state(seeded, stub_provider) -> None:
    del stub_provider
    state = URLSafeTimedSerializer(
        get_settings().yaaos_invitation_token_secret.get_secret_value(), salt="yaaos-integration-connect"
    ).dumps(
        {
            "org_id": str(seeded["org"].id),
            "user_initiating": str(seeded["admin"].id),
            "provider": "stub",
        }
    )
    async with _client() as c:
        # State says "stub", URL path says "other" → mismatch.
        r = await c.get(
            "/api/mcp-proxy/other/callback",
            params={"code": "abc", "state": state},
            follow_redirects=False,
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_endpoint_clears_row(seeded, stub_provider, stub_exchange, db_session) -> None:
    del stub_provider, stub_exchange
    # Seed a credential by hitting the callback first.
    state = URLSafeTimedSerializer(
        get_settings().yaaos_invitation_token_secret.get_secret_value(), salt="yaaos-integration-connect"
    ).dumps(
        {
            "org_id": str(seeded["org"].id),
            "user_initiating": str(seeded["admin"].id),
            "provider": "stub",
        }
    )
    sess = seeded["admin_sess"]
    async with _client() as c:
        await c.get(
            "/api/mcp-proxy/stub/callback",
            params={"code": "abc", "state": state},
            follow_redirects=False,
        )
        r = await c.delete(
            "/api/mcp-proxy/stub",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"removed": True}


@pytest.mark.asyncio
async def test_patch_endpoint_updates_allowlist_and_enabled(
    seeded, stub_provider, stub_exchange, db_session
) -> None:
    del stub_provider, stub_exchange
    state = URLSafeTimedSerializer(
        get_settings().yaaos_invitation_token_secret.get_secret_value(), salt="yaaos-integration-connect"
    ).dumps(
        {
            "org_id": str(seeded["org"].id),
            "user_initiating": str(seeded["admin"].id),
            "provider": "stub",
        }
    )
    sess = seeded["admin_sess"]
    async with _client() as c:
        await c.get(
            "/api/mcp-proxy/stub/callback",
            params={"code": "abc", "state": state},
            follow_redirects=False,
        )
        r = await c.patch(
            "/api/mcp-proxy/stub",
            json={"allowed_tools": ["update_thing"], "enabled": False},
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed_tools"] == ["update_thing"]
    assert body["enabled"] is False
    # Audit row was written.
    rows = await list_for_org(org_id=seeded["org"].id, actions=["mcp.stub.allowlist_updated"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_validate_endpoint_returns_provider_result(seeded, stub_provider, stub_exchange) -> None:
    del stub_exchange
    state = URLSafeTimedSerializer(
        get_settings().yaaos_invitation_token_secret.get_secret_value(), salt="yaaos-integration-connect"
    ).dumps(
        {
            "org_id": str(seeded["org"].id),
            "user_initiating": str(seeded["admin"].id),
            "provider": "stub",
        }
    )
    sess = seeded["admin_sess"]
    async with _client() as c:
        await c.get(
            "/api/mcp-proxy/stub/callback",
            params={"code": "abc", "state": state},
            follow_redirects=False,
        )
        stub_provider.validate_returns = True
        r_ok = await c.post(
            "/api/mcp-proxy/stub/validate",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
        assert r_ok.json() == {"valid": True}

        stub_provider.validate_returns = False
        r_bad = await c.post(
            "/api/mcp-proxy/stub/validate",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
        assert r_bad.json() == {"valid": False}


@pytest.mark.asyncio
async def test_validate_endpoint_404_when_not_connected(seeded, stub_provider) -> None:
    del stub_provider
    sess = seeded["admin_sess"]
    async with _client() as c:
        r = await c.post(
            "/api/mcp-proxy/stub/validate",
            cookies={"yaaos_session": sess.raw_token, "yaaos_csrf": sess.csrf_token},
            headers={"X-Org-Slug": seeded["org"].slug, "X-CSRF-Token": sess.csrf_token},
        )
    assert r.status_code == 404
