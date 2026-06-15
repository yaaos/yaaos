"""Service-tier guards for the hourly `integrations_health_check` `@scheduled` task.

Two invariants:
  - The body is registered with the taskiq broker under the public task name.
  - `run_health_check_once` runs end-to-end against a real DB and sends one
    broken-creds email when validation fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.auth import Role
from app.core.identity import repository as identity_repo
from app.core.oauth import ProviderConfig
from app.core.secrets import encrypt
from app.core.tasks import get_broker
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.scheduler import integrations_health_check, run_health_check_once
from app.domain.integrations.types import _REGISTRY
from app.domain.orgs import repository as orgs_repo
from app.testing.seed import read_email_inbox

pytestmark = pytest.mark.service

_TASK_NAME = "integrations_health_check"


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
    provider_id: str = "stub_sched_svc"
    config: ProviderConfig = field(default_factory=_config)
    next_validate: bool = True

    async def validate(self, access_token: SecretStr) -> bool:
        del access_token
        return self.next_validate


@pytest.fixture
def stub_provider():
    provider = _StubProvider()
    _REGISTRY["stub_sched_svc"] = provider
    try:
        yield provider
    finally:
        _REGISTRY.pop("stub_sched_svc", None)


async def _seed(db_session, *, owner_email: str = "owner-svc@example.com"):
    org = await orgs_repo.insert_org(db_session, slug=f"sched-svc-{datetime.now(UTC).timestamp()}")
    owner = await identity_repo.insert_user(db_session, display_name="Owner")
    await identity_repo.add_email(
        db_session, user_id=owner.id, email=owner_email, is_primary=True, verified=True
    )
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.org_id, role=Role.OWNER, handle="own"
    )
    row = McpCredentialRow(
        org_id=org.org_id,
        provider="stub_sched_svc",
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
async def test_integrations_health_check_task_registered_with_broker() -> None:
    """The health-check body is registered with the broker under its public
    task name. Regression guard for the `@scheduled` decorator wiring."""
    assert get_broker().find_task(_TASK_NAME) is not None
    assert integrations_health_check is not None


@pytest.mark.asyncio
async def test_health_check_body_sends_email_on_broken_creds(db_session, stub_provider) -> None:
    """Drive `run_health_check_once` directly — validation failure flips status
    and sends one broken-creds email to the org owner."""
    inbox = read_email_inbox()
    stub_provider.next_validate = False
    org, _ = await _seed(db_session, owner_email="broken-owner@example.com")

    counts = await run_health_check_once()

    assert counts["failed"] >= 1
    assert counts["notified"] == 1

    refreshed = (
        await db_session.execute(select(McpCredentialRow).where(McpCredentialRow.org_id == org.org_id))
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.last_refresh_status == "failed"
    assert any(m.to == "broken-owner@example.com" for m in inbox)
