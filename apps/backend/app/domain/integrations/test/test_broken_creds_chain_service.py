"""Service test (Phase 2 gap C): integration health-check → audit + email → next review's proxy → review-output prefix.

The six broken-creds surfaces described in the architecture are tested in
isolation today. This service test stitches the BACKEND half of the chain —
health-check flips the credential status, audits, emails Owners; then the next
review's proxy dispatch hits the broken row, records the provider, and the
reviewer's review-output prefix composes the warning.

(The three frontend surfaces — banner, settings badge, Claude Code warning
block — are vitest + e2e concerns.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.core.audit_log import list_for_org
from app.core.auth import AuthMiddleware
from app.core.oauth import ProviderConfig
from app.core.secrets import encrypt
from app.domain.identity import repository as identity_repo
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.scheduler import run_health_check_once
from app.domain.integrations.types import _REGISTRY
from app.domain.mcp_proxy import consume_broken_creds, mint_token
from app.domain.mcp_proxy import web as _mcp_web  # noqa: F401  (route registration)
from app.domain.orgs import repository as orgs_repo
from app.domain.orgs.email import get_test_inbox
from app.domain.orgs.types import Role
from app.domain.pull_requests.models import PullRequestRow
from app.domain.reviewer.mcp_wiring import (
    prefix_broken_creds_warning as _prefix_broken_creds_warning,
)
from app.domain.reviewer.models import ReviewRow
from app.domain.tickets.models import TicketRow


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
        known_read_tools=("get_issue",),
        known_write_tools=("update_issue",),
    )


@dataclass
class _StubProvider:
    provider_id: str = "stub_chain"
    config: ProviderConfig = field(default_factory=_config)
    next_validate: bool = False

    async def validate(self, access_token: SecretStr) -> bool:
        del access_token
        return self.next_validate


@pytest.fixture
def stub_provider():
    provider = _StubProvider()
    _REGISTRY["stub_chain"] = provider
    try:
        yield provider
    finally:
        _REGISTRY.pop("stub_chain", None)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"mcp"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


@pytest.mark.service
@pytest.mark.asyncio
async def test_health_check_flip_then_next_review_dispatches_through_broken_creds(
    db_session, stub_provider
) -> None:
    """End-to-end backend chain:

    1. Seed an org + Owner with a verified email + a connected (initially "ok") credential.
    2. Stub provider's `validate` returns False (creds went bad upstream).
    3. `run_health_check_once()` flips status to "failed", audits, emails the Owner.
    4. A review is in flight. The agent calls a tool via the proxy.
    5. Proxy sees `last_refresh_status="failed"` → returns `broken_creds` JSON-RPC error
       AND records the provider in the per-review broken-creds tracker.
    6. The reviewer drains the tracker and the review-summary prefix carries the provider.
    """
    inbox = get_test_inbox()
    inbox.clear()
    stub_provider.next_validate = False

    # 1. Seed org + Owner + email + credential (initially "ok").
    org = await orgs_repo.insert_org(db_session, slug=f"svc-chain-{uuid4().hex[:8]}")
    owner = await identity_repo.insert_user(db_session, display_name="Owner")
    await identity_repo.add_email(
        db_session, user_id=owner.id, email="owner@example.com", is_primary=True, verified=True
    )
    await orgs_repo.insert_membership(
        db_session, user_id=owner.id, org_id=org.id, role=Role.OWNER, handle="owner"
    )
    db_session.add(
        McpCredentialRow(
            org_id=org.id,
            provider="stub_chain",
            encrypted_access_token=encrypt("upstream-access").decode(),
            encrypted_refresh_token=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["read"],
            allowed_tools=[],
            enabled=True,
            upstream_identity="stub-bot",
            last_refresh_status="ok",
            last_validated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    # 2 + 3. Health-check flips status, audits, emails.
    counts = await run_health_check_once()
    assert counts["failed"] == 1
    assert counts["notified"] == 1
    assert any(m.to == "owner@example.com" for m in inbox)

    failure_audits = await list_for_org(org_id=org.id, actions=["mcp.stub_chain.token_refresh_failed"])
    assert len(failure_audits) == 1

    # 4. Seed a review + mint its bearer.
    ticket = TicketRow(
        id=uuid4(),
        org_id=org.id,
        source="github_pr",
        source_external_id=f"pr-{uuid4()}",
        title="t",
        plugin_id="github",
        repo_external_id="owner/repo",
    )
    db_session.add(ticket)
    await db_session.flush()
    pr = PullRequestRow(
        id=uuid4(),
        org_id=org.id,
        plugin_id="github",
        repo_external_id="owner/repo",
        external_id=ticket.source_external_id,
        number=1,
        title="t",
        body=None,
        author_login="a",
        author_type="user",
        base_branch="main",
        head_branch="b",
        base_sha="0",
        head_sha="1",
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="http://test",
        ticket_id=ticket.id,
    )
    db_session.add(pr)
    await db_session.flush()
    review = ReviewRow(
        id=uuid4(),
        org_id=org.id,
        pr_id=pr.id,
        sequence_number=1,
        status="running",
        trigger_reason="manual_full",
        destination="vcs",
    )
    db_session.add(review)
    await db_session.flush()
    raw_token = await mint_token(review.id, session=db_session)
    await db_session.commit()

    # 5. Agent calls a tool through the proxy. Proxy sees broken creds.
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_chain",
            headers={"Authorization": f"Bearer {raw_token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
    assert r.status_code == 200
    assert r.json()["error"]["data"]["code"] == "broken_creds"

    # 6. Reviewer drains the tracker and prefixes the summary.
    observed = consume_broken_creds(review.id)
    assert observed == {"stub_chain"}
    summary = _prefix_broken_creds_warning("Body.", sorted(observed))
    assert summary is not None
    assert "**stub_chain**" in summary
