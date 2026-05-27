"""Health-check loop: flips status + audits + dedups owner notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.audit_log import list_for_org
from app.core.oauth import ProviderConfig
from app.core.secrets import encrypt
from app.domain.identity import repository as identity_repo
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.scheduler import run_health_check_once
from app.domain.integrations.types import _REGISTRY
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.email import get_test_inbox
from app.domain.orgs.types import Role

# Drives the hourly health-check loop end-to-end: provider.validate →
# `mcp_credentials.last_refresh_status` flip → audit row → owner email.
# Crosses scheduler + integrations + audit_log + orgs.email + identity. Service tier.
pytestmark = pytest.mark.service


def _config() -> ProviderConfig:
    return ProviderConfig(
        authorize_url="https://stub.test/authorize",
        token_url="https://stub.test/token",
        refresh_url="https://stub.test/token",
        mcp_url="https://stub.test/mcp",
        client_id="cid",
        client_secret=SecretStr("csecret"),
        scope_separator=" ",
        default_scopes=("read",),
        known_read_tools=("get",),
        known_write_tools=("update",),
    )


@dataclass
class _StubProvider:
    provider_id: str = "stub_sched"
    config: ProviderConfig = field(default_factory=_config)
    next_validate: bool = True

    async def validate(self, access_token: SecretStr) -> bool:
        del access_token
        return self.next_validate


@pytest.fixture
def stub_provider():
    provider = _StubProvider()
    _REGISTRY["stub_sched"] = provider
    try:
        yield provider
    finally:
        _REGISTRY.pop("stub_sched", None)


async def _seed(db_session, *, owner_email: str | None = "owner@example.com"):
    org = await orgs_repo.insert_org(db_session, slug=f"sched-{datetime.now(UTC).timestamp()}")
    if owner_email is not None:
        owner = await identity_repo.insert_user(db_session, display_name="Owner")
        await identity_repo.add_email(
            db_session, user_id=owner.id, email=owner_email, is_primary=True, verified=True
        )
        await orgs_repo.insert_membership(
            db_session, user_id=owner.id, org_id=org.id, role=Role.OWNER, handle="own"
        )
    row = McpCredentialRow(
        org_id=org.id,
        provider="stub_sched",
        encrypted_access_token=encrypt("access-1").decode(),
        encrypted_refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["read"],
        allowed_tools=[],
        enabled=True,
        upstream_identity="stub-bot",
        last_refresh_status="ok",
        last_validated_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()
    return org, row


@pytest.mark.asyncio
async def test_validate_success_keeps_status_ok(db_session, stub_provider) -> None:
    stub_provider.next_validate = True
    org, _ = await _seed(db_session)
    counts = await run_health_check_once()
    assert counts["ok"] >= 1
    refreshed = (
        await db_session.execute(select(McpCredentialRow).where(McpCredentialRow.org_id == org.id))
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.last_refresh_status == "ok"


@pytest.mark.asyncio
async def test_validate_failure_flips_status_audits_and_notifies(db_session, stub_provider) -> None:
    inbox = get_test_inbox()
    inbox.clear()
    stub_provider.next_validate = False
    org, _ = await _seed(db_session, owner_email="o1@example.com")

    counts = await run_health_check_once()
    assert counts["failed"] >= 1
    assert counts["notified"] == 1

    refreshed = (
        await db_session.execute(select(McpCredentialRow).where(McpCredentialRow.org_id == org.id))
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.last_refresh_status == "failed"
    assert refreshed.last_refresh_failed_at is not None
    assert refreshed.last_failure_notified_at is not None

    audits = await list_for_org(org_id=org.id, actions=["mcp.stub_sched.token_refresh_failed"])
    assert len(audits) == 1
    assert any(m.to == "o1@example.com" for m in inbox)


@pytest.mark.asyncio
async def test_failure_dedups_within_24h(db_session, stub_provider) -> None:
    inbox = get_test_inbox()
    inbox.clear()
    stub_provider.next_validate = False
    _org, _row = await _seed(db_session, owner_email="o2@example.com")

    counts1 = await run_health_check_once()
    counts2 = await run_health_check_once()
    assert counts1["notified"] == 1
    # Second pass still finds the row failed, but the 24h dedup suppresses email.
    assert counts2["notified"] == 0


@pytest.mark.asyncio
async def test_failure_resends_after_dedup_window(db_session, stub_provider) -> None:
    inbox = get_test_inbox()
    inbox.clear()
    stub_provider.next_validate = False
    org, _ = await _seed(db_session, owner_email="o3@example.com")

    await run_health_check_once()
    # Backdate the notified-at by 25h and re-run; second email should fire.
    row = (
        await db_session.execute(select(McpCredentialRow).where(McpCredentialRow.org_id == org.id))
    ).scalar_one()
    row.last_failure_notified_at = datetime.now(UTC) - timedelta(hours=25)
    await db_session.commit()

    counts2 = await run_health_check_once()
    assert counts2["notified"] == 1
