"""MCP wiring: `_build_mcp_payload` + mint/revoke + agent_config threading.

The full reviewer worker is exercised by the e2e suite. These tests
cover the small, deterministic surface: provider collection + token lifecycle
hooked into the same payload the worker hands the coding-agent plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.oauth import ProviderConfig
from app.core.vcs import VCSPullRequest
from app.domain.integrations import _REGISTRY, create_credential
from app.domain.mcp_proxy import get_token_by_hash, hash_token
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer import (
    PRReviewAggregate,
    ReviewScope,
    ReviewTrigger,
    SqlAlchemyAggregateRepository,
)
from app.domain.reviewer.mcp_wiring import build_mcp_payload as _build_mcp_payload
from app.domain.reviewer.models import ReviewRow
from app.domain.tickets import create as create_ticket
from app.domain.tickets import upsert as upsert_pr


def _stub_config() -> ProviderConfig:
    return ProviderConfig(
        authorize_url="https://stub.test/authorize",
        token_url="https://stub.test/token",
        refresh_url="https://stub.test/token",
        mcp_url="https://stub.test/mcp",
        client_id="cid",
        client_secret=SecretStr("csecret"),
        scope_separator=" ",
        default_scopes=("read",),
        known_read_tools=("get_issue", "search"),
        known_write_tools=("update_issue", "create_comment"),
    )


@dataclass
class _StubProvider:
    provider_id: str
    config: ProviderConfig = field(default_factory=_stub_config)

    async def validate(self, access_token: SecretStr) -> bool:
        del access_token
        return True


@pytest.fixture
def stub_providers():
    """Register two stub providers for the duration of one test."""
    prior_keys = set(_REGISTRY.keys())
    _REGISTRY["linear_stub"] = _StubProvider(provider_id="linear_stub")
    _REGISTRY["notion_stub"] = _StubProvider(provider_id="notion_stub")
    try:
        yield
    finally:
        for k in set(_REGISTRY.keys()) - prior_keys:
            _REGISTRY.pop(k, None)


async def _seed_org(db_session, slug: str):
    return await orgs_repo.insert_org(db_session, slug=slug)


async def _seed_review_row(db_session, *, org_id):
    """Insert the minimal ticket → PR → review chain so `mcp_review_tokens`
    FKs resolve. Returns the review row."""
    ext_id = f"pr-{uuid4()}"
    ticket_id, _ = await create_ticket(
        type="pr_review",
        payload={},
        idempotency_key=ext_id,
        org_id=org_id,
        title="t",
        source="github_pr",
        source_external_id=ext_id,
        plugin_id="github",
        repo_external_id="owner/repo",
        session=db_session,
    )
    pr = await upsert_pr(
        VCSPullRequest(
            plugin_id="github",
            repo_external_id="owner/repo",
            external_id=ext_id,
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
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        ticket_id=ticket_id,
        org_id=org_id,
        session=db_session,
    )
    agg = PRReviewAggregate(pr_id=pr.id, org_id=org_id)
    review = agg.start_review(
        trigger=ReviewTrigger.MANUAL_FULL,
        scope=ReviewScope.full(base_sha="0", head_sha="1"),
        commit_sha="1",
    )
    repo = SqlAlchemyAggregateRepository(db_session)
    await repo.save(agg)
    # Return the raw ReviewRow for the FKs
    return await db_session.get(ReviewRow, review.id)


async def _seed_credential(
    db_session,
    *,
    org_id,
    provider: str,
    enabled: bool = True,
    allowed_tools: list[str] | None = None,
    last_refresh_status: str = "ok",
) -> None:
    await create_credential(
        db_session,
        org_id=org_id,
        provider=provider,
        encrypted_access_token="enc-access",
        expires_at=datetime.now(UTC),
        scopes=["read"],
        allowed_tools=allowed_tools or [],
        enabled=enabled,
        upstream_identity=f"{provider}-bot",
        last_refresh_status=last_refresh_status,
    )


@pytest.mark.asyncio
async def test_no_connected_providers_returns_none(db_session, stub_providers) -> None:
    del stub_providers
    org = await _seed_org(db_session, slug="mcp-none")
    await db_session.commit()
    payload = await _build_mcp_payload(uuid4(), org_id=org.org_id)
    assert payload is None


@pytest.mark.asyncio
async def test_disabled_provider_excluded(db_session, stub_providers) -> None:
    del stub_providers
    org = await _seed_org(db_session, slug="mcp-disabled")
    await _seed_credential(db_session, org_id=org.org_id, provider="linear_stub", enabled=False)
    await db_session.commit()
    payload = await _build_mcp_payload(uuid4(), org_id=org.org_id)
    assert payload is None


@pytest.mark.asyncio
async def test_broken_creds_provider_excluded(db_session, stub_providers) -> None:
    del stub_providers
    org = await _seed_org(db_session, slug="mcp-broken")
    await _seed_credential(
        db_session,
        org_id=org.org_id,
        provider="linear_stub",
        last_refresh_status="failed",
    )
    await db_session.commit()
    payload = await _build_mcp_payload(uuid4(), org_id=org.org_id)
    assert payload is None


@pytest.mark.asyncio
async def test_connected_provider_mints_token_and_surfaces_servers(db_session, stub_providers) -> None:
    del stub_providers
    org = await _seed_org(db_session, slug="mcp-connected")
    review = await _seed_review_row(db_session, org_id=org.org_id)
    await _seed_credential(
        db_session,
        org_id=org.org_id,
        provider="linear_stub",
        allowed_tools=["update_issue"],
    )
    await _seed_credential(db_session, org_id=org.org_id, provider="notion_stub", allowed_tools=[])
    await db_session.commit()

    payload = await _build_mcp_payload(review.id, org_id=org.org_id)
    assert payload is not None
    assert payload["base_url"].endswith(f"/api/mcp/{review.id}")

    providers = {s["provider"]: s for s in payload["servers"]}
    assert set(providers.keys()) == {"linear_stub", "notion_stub"}
    assert providers["linear_stub"]["allowed_tools"] == ["update_issue"]
    assert "get_issue" in providers["linear_stub"]["known_read_tools"]

    # The minted token is persisted with its sha256 hash and tagged to review_id.
    token_hash = hash_token(payload["token"])
    row = await get_token_by_hash(token_hash, session=db_session)
    assert row is not None
    assert row.review_id == review.id
