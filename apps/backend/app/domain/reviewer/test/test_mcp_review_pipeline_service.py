"""Service test: reviewer → MCP proxy → broken-creds tracker → review-output prefix.

Stitches three pieces that are tested in isolation today:

- `mcp_proxy.mint_token(review_id)` → bearer for the workspace.
- `POST /api/mcp/{review_id}/{server}` → proxy dispatch + audit row + `record_broken_creds` side-effect.
- `_prefix_broken_creds_warning(...)` → yellow GitHub callout on the review summary.

Existing unit/HTTP-boundary tests cover each in isolation
(`test_dispatch.py`, `test_broken_creds_prefix.py`). This service test asserts
they actually compose into the contract the reviewer relies on.
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
from app.core.identity import repository as identity_repo
from app.core.oauth import ProviderConfig
from app.core.secrets import encrypt
from app.core.vcs import VCSPullRequest
from app.domain.integrations import _REGISTRY, create_credential
from app.domain.mcp_proxy import consume_broken_creds, mint_token
from app.domain.mcp_proxy import web as _mcp_web  # noqa: F401  (route registration)
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer import PRReviewAggregate, ReviewScope, ReviewTrigger, SqlAlchemyAggregateRepository
from app.domain.reviewer.mcp_wiring import (
    prefix_broken_creds_warning as _prefix_broken_creds_warning,
)
from app.domain.reviewer.models import ReviewRow
from app.domain.tickets import create as create_ticket
from app.domain.tickets import upsert as upsert_pr


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
    provider_id: str = "stub_pipeline"
    config: ProviderConfig = field(default_factory=_config)

    async def validate(self, access_token: SecretStr) -> bool:
        del access_token
        return True


@pytest.fixture
def stub_provider():
    _REGISTRY["stub_pipeline"] = _StubProvider()
    try:
        yield
    finally:
        _REGISTRY.pop("stub_pipeline", None)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"mcp"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _seed_review_with_broken_credential(db_session) -> tuple[ReviewRow, str]:
    """Seed org + ticket + PR + review + a `last_refresh_status="failed"` credential.
    Mints the per-review bearer and returns (review, raw_token)."""
    org = await orgs_repo.insert_org(db_session, slug=f"svc-mcp-{uuid4().hex[:8]}")
    await identity_repo.insert_user(db_session, display_name="U")
    ext_id = f"pr-{uuid4()}"
    ticket_id, _ = await create_ticket(
        type="pr_review",
        payload={},
        idempotency_key=ext_id,
        org_id=org.org_id,
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
        org_id=org.org_id,
        session=db_session,
    )
    agg = PRReviewAggregate(pr_id=pr.id, org_id=org.org_id)
    _review = agg.start_review(
        trigger=ReviewTrigger.MANUAL_FULL,
        scope=ReviewScope.full(base_sha="0", head_sha="1"),
        commit_sha="1",
    )
    repo = SqlAlchemyAggregateRepository(db_session)
    await repo.save(agg)
    review = await db_session.get(ReviewRow, _review.id)
    await create_credential(
        db_session,
        org_id=org.org_id,
        provider="stub_pipeline",
        encrypted_access_token=encrypt("upstream-access").decode(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["read"],
        allowed_tools=[],
        enabled=True,
        upstream_identity="stub-bot",
        last_refresh_status="failed",
        last_refresh_failed_at=datetime.now(UTC),
    )
    raw = await mint_token(review.id, org_id=org.org_id, session=db_session)
    await db_session.commit()
    return review, raw


@pytest.mark.service
@pytest.mark.asyncio
async def test_review_with_broken_creds_yields_prefixed_summary(db_session, stub_provider) -> None:
    """Composition contract:

    1. Mint bearer for a review.
    2. Coding-agent calls a tool through the proxy.
    3. Provider's credentials are broken → proxy returns `broken_creds` error.
    4. `record_broken_creds` side-effect captures the provider in the per-review tracker.
    5. Reviewer drains via `consume_broken_creds(review.id)` and prefixes the GitHub summary.
    """
    del stub_provider
    review, token = await _seed_review_with_broken_credential(db_session)

    # Coding-agent calls a tool via the proxy.
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_issue", "arguments": {"id": "LIN-1"}},
    }
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_pipeline",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 200
    assert r.json()["error"]["data"]["code"] == "broken_creds"

    # The proxy recorded the provider for the reviewer to drain.
    observed = consume_broken_creds(review.id)
    assert observed == {"stub_pipeline"}

    # Reviewer's review-output prefix composes the warning into the summary.
    summary_with_prefix = _prefix_broken_creds_warning(
        "Original review body.",
        sorted(observed),
    )
    assert summary_with_prefix is not None
    assert summary_with_prefix.startswith("> [!WARNING]")
    assert "**stub_pipeline**" in summary_with_prefix
    assert "Original review body." in summary_with_prefix

    # No `dispatched` audit row because the proxy short-circuited on broken_creds.
    audits = await list_for_org(org_id=review.org_id, actions=["mcp.stub_pipeline.dispatched"])
    assert audits == []
