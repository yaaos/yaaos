"""Coverage for `domain/integrations` service: connect_callback, clear,
validate, update_allowlist. Each test stubs the IntegrationProvider into
the registry so no real HTTP fires."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pydantic import SecretStr

from app.core.audit_log import Actor, list_for_org
from app.core.identity import repository as identity_repo
from app.core.oauth import ProviderConfig, Tokens
from app.core.secrets import decrypt
from app.domain import integrations as integ
from app.domain.integrations.types import _REGISTRY, IntegrationNotConnectedError
from app.domain.orgs import repository as orgs_repo


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
    """Replace `core/oauth.exchange_code` so connect_callback doesn't hit
    the network."""

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


@pytest.fixture
async def seeded(db_session):
    user = await identity_repo.insert_user(db_session, display_name="U")
    org = await orgs_repo.insert_org(db_session, slug="integ-test")
    return {"user": user, "org": org, "actor": Actor.user(user_id=user.id)}


@pytest.mark.asyncio
async def test_connect_callback_persists_encrypted_tokens(
    seeded, stub_provider, stub_exchange, db_session
) -> None:
    del stub_exchange
    row = await integ.connect_callback(
        db_session,
        provider="stub",
        code="abc",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
        upstream_identity="service@example.test",
    )
    # Tokens are encrypted at rest; decrypt only at the call site.
    assert row.encrypted_access_token != "access-1"
    assert decrypt(row.encrypted_access_token.encode()) == b"access-1"
    assert row.encrypted_refresh_token is not None
    assert decrypt(row.encrypted_refresh_token.encode()) == b"refresh-1"
    assert row.upstream_identity == "service@example.test"
    assert row.last_refresh_status == "ok"


@pytest.mark.asyncio
async def test_connect_callback_emits_audit(seeded, stub_provider, stub_exchange, db_session) -> None:
    del stub_exchange
    await integ.connect_callback(
        db_session,
        provider="stub",
        code="abc",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
    )
    rows = await list_for_org(org_id=seeded["org"].id, actions=["mcp.stub.connected"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_connect_callback_reconnect_keeps_allowlist(
    seeded, stub_provider, stub_exchange, db_session
) -> None:
    del stub_exchange
    await integ.connect_callback(
        db_session,
        provider="stub",
        code="abc",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
    )
    await integ.update_allowlist(
        db_session,
        org_id=seeded["org"].id,
        provider="stub",
        allowed_tools=["update_thing"],
        actor=seeded["actor"],
    )
    # Reconnecting (e.g. token refresh failed → user re-OAuths) must not
    # zero out the operator's allowlist.
    row = await integ.connect_callback(
        db_session,
        provider="stub",
        code="def",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
    )
    assert row.allowed_tools == ["update_thing"]


@pytest.mark.asyncio
async def test_clear_removes_and_audits(seeded, stub_provider, stub_exchange, db_session) -> None:
    del stub_exchange
    await integ.connect_callback(
        db_session,
        provider="stub",
        code="abc",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
    )
    removed = await integ.clear(db_session, org_id=seeded["org"].id, provider="stub", actor=seeded["actor"])
    assert removed is True
    assert await integ.get(db_session, seeded["org"].id, "stub") is None
    rows = await list_for_org(org_id=seeded["org"].id, actions=["mcp.stub.disconnected"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_clear_no_op_returns_false_no_audit(seeded, db_session) -> None:
    removed = await integ.clear(db_session, org_id=seeded["org"].id, provider="stub", actor=seeded["actor"])
    assert removed is False
    rows = await list_for_org(org_id=seeded["org"].id, actions=["mcp.stub.disconnected"])
    assert rows == []


@pytest.mark.asyncio
async def test_validate_flips_status_on_failure(seeded, stub_provider, stub_exchange, db_session) -> None:
    del stub_exchange
    await integ.connect_callback(
        db_session,
        provider="stub",
        code="abc",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
    )
    stub_provider.validate_returns = False
    ok = await integ.validate(db_session, org_id=seeded["org"].id, provider="stub", actor=seeded["actor"])
    assert ok is False
    row = await integ.get(db_session, seeded["org"].id, "stub")
    assert row is not None
    assert row.last_refresh_status == "failed"
    assert row.last_refresh_failed_at is not None


@pytest.mark.asyncio
async def test_validate_recovers_status_on_success(seeded, stub_provider, stub_exchange, db_session) -> None:
    del stub_exchange
    await integ.connect_callback(
        db_session,
        provider="stub",
        code="abc",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
    )
    # Mark broken first.
    stub_provider.validate_returns = False
    await integ.validate(db_session, org_id=seeded["org"].id, provider="stub", actor=seeded["actor"])
    # Now recover.
    stub_provider.validate_returns = True
    ok = await integ.validate(db_session, org_id=seeded["org"].id, provider="stub", actor=seeded["actor"])
    assert ok is True
    row = await integ.get(db_session, seeded["org"].id, "stub")
    assert row is not None
    assert row.last_refresh_status == "ok"
    assert row.last_refresh_failed_at is None


@pytest.mark.asyncio
async def test_validate_missing_install_raises(seeded, stub_provider, db_session) -> None:
    with pytest.raises(IntegrationNotConnectedError):
        await integ.validate(db_session, org_id=seeded["org"].id, provider="stub", actor=seeded["actor"])


@pytest.mark.asyncio
async def test_update_allowlist_replaces_and_audits(seeded, stub_provider, stub_exchange, db_session) -> None:
    del stub_exchange
    await integ.connect_callback(
        db_session,
        provider="stub",
        code="abc",
        org_id=seeded["org"].id,
        redirect_uri="http://test/cb",
        actor=seeded["actor"],
    )
    row = await integ.update_allowlist(
        db_session,
        org_id=seeded["org"].id,
        provider="stub",
        allowed_tools=["update_thing", "ghost_tool"],
        actor=seeded["actor"],
    )
    assert row.allowed_tools == ["update_thing", "ghost_tool"]
    # Replacement, not append:
    row = await integ.update_allowlist(
        db_session,
        org_id=seeded["org"].id,
        provider="stub",
        allowed_tools=[],
        actor=seeded["actor"],
    )
    assert row.allowed_tools == []
    rows = await list_for_org(org_id=seeded["org"].id, actions=["mcp.stub.allowlist_updated"])
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_list_broken_credentials_for_org(seeded, db_session) -> None:
    """Only enabled + failed credentials appear; ok + disabled variants are excluded."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.domain.integrations.models import McpCredentialRow  # noqa: PLC0415

    org_id = seeded["org"].id

    def _row(provider: str, *, enabled: bool, status: str) -> McpCredentialRow:
        return McpCredentialRow(
            org_id=org_id,
            provider=provider,
            encrypted_access_token="enc",
            encrypted_refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["read"],
            allowed_tools=[],
            enabled=enabled,
            upstream_identity=f"{provider}-bot",
            last_refresh_status=status,
            last_refresh_failed_at=datetime.now(UTC) if status == "failed" else None,
        )

    # enabled + failed → should appear
    db_session.add(_row("linear", enabled=True, status="failed"))
    # enabled + ok → excluded
    db_session.add(_row("notion", enabled=True, status="ok"))
    # disabled + failed → excluded
    db_session.add(_row("jira", enabled=False, status="failed"))
    await db_session.flush()

    result = await integ.list_broken_credentials_for_org(db_session, org_id)
    assert len(result) == 1
    assert result[0].provider == "linear"
    assert result[0].enabled is True
    assert result[0].last_refresh_status == "failed"
    assert result[0].last_refresh_failed_at is not None
