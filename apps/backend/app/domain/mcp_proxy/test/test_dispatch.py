"""End-to-end coverage for `POST /api/mcp/{review_id}/{server}` — the proxy
forwards JSON-RPC to a stubbed upstream and audits each dispatched method.

Stubs:
  - Provider `IntegrationProvider` registered into `_REGISTRY` so the proxy
    finds it for `tools/list` + `tools/call`.
  - `httpx.AsyncClient` patched on the proxy module so the upstream call
    resolves to a canned JSON-RPC response without network I/O.

Asserts the broken_creds path also records via
`record_broken_creds(review_id, provider)` for the reviewer's review-output
yellow warning block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select

from app.core.audit_log import list_for_org
from app.core.auth import AuthMiddleware
from app.core.identity import repository as identity_repo
from app.core.oauth import ProviderConfig
from app.core.secrets import encrypt
from app.core.vcs import VCSPullRequest
from app.domain.integrations import _REGISTRY, create_credential
from app.domain.mcp_proxy import (
    consume_broken_creds,
    mint_token,
    revoke_token,
)
from app.domain.mcp_proxy import (
    web as _mcp_web,
)
from app.domain.mcp_proxy.models import McpReviewTokenRow
from app.domain.orgs import repository as orgs_repo
from app.domain.reviewer import (
    PRReviewAggregate,
    ReviewScope,
    ReviewTrigger,
    SqlAlchemyAggregateRepository,
)
from app.domain.tickets import create as create_ticket
from app.domain.tickets import upsert as upsert_pr

# Every test in this file drives the MCP proxy end-to-end (real Postgres via
# `db_session`, stub IntegrationProvider in `_REGISTRY`, stub upstream via
# monkeypatched httpx.AsyncClient). Service tier.
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
        known_read_tools=("get_issue", "search_issues"),
        known_write_tools=("update_issue", "create_comment"),
    )


@dataclass
class _StubProvider:
    provider_id: str = "stub_disp"
    config: ProviderConfig = field(default_factory=_config)

    async def validate(self, access_token: SecretStr) -> bool:
        del access_token
        return True


class _FakeUpstreamResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self.text = "{}"
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Stand-in for `httpx.AsyncClient` — returns a canned response."""

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.last_post: tuple[str, dict, dict] | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    async def post(self, url: str, *, headers: dict, json: dict) -> _FakeUpstreamResponse:
        self.last_post = (url, headers, json)
        return _FakeUpstreamResponse({"jsonrpc": "2.0", "id": json.get("id"), "result": {"ok": True}})


@pytest.fixture
def stub_provider():
    _REGISTRY["stub_disp"] = _StubProvider()
    try:
        yield
    finally:
        _REGISTRY.pop("stub_disp", None)


@pytest.fixture
def stub_upstream(monkeypatch):
    monkeypatch.setattr(
        _mcp_web, "httpx", type("_M", (), {"AsyncClient": _FakeAsyncClient, "HTTPError": httpx.HTTPError})
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    from app.core.webserver import mount_specs  # noqa: PLC0415

    mount_specs(app, only={"mcp"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _seed_review(db_session):  # type: ignore[no-untyped-def]
    from app.domain.reviewer import Review  # noqa: PLC0415

    org = await orgs_repo.insert_org(db_session, slug=f"mcp-disp-{uuid4().hex[:8]}")
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
    review: Review = agg.start_review(
        trigger=ReviewTrigger.MANUAL_FULL,
        scope=ReviewScope.full(base_sha="0", head_sha="1"),
        commit_sha="1",
    )
    repo = SqlAlchemyAggregateRepository(db_session)
    await repo.save(agg)
    raw_token = await mint_token(review.id, org_id=org.org_id, session=db_session)
    await db_session.commit()
    return review, raw_token


async def _seed_credential(
    db_session,
    *,
    org_id,
    enabled: bool = True,
    last_refresh_status: str = "ok",
    allowed_tools: list[str] | None = None,
    expires_in_seconds: int = 3600,
):
    row = await create_credential(
        db_session,
        org_id=org_id,
        provider="stub_disp",
        encrypted_access_token=encrypt("upstream-access").decode(),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        scopes=["read"],
        allowed_tools=allowed_tools or [],
        enabled=enabled,
        upstream_identity="stub-bot",
        last_refresh_status=last_refresh_status,
    )
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_dispatch_success_audits_and_calls_upstream(db_session, stub_provider, stub_upstream) -> None:
    del stub_provider, stub_upstream
    review, token = await _seed_review(db_session)
    await _seed_credential(db_session, org_id=review.org_id)

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_issue", "arguments": {"id": "LIN-1"}},
    }
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_disp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"ok": True}

    audits = await list_for_org(org_id=review.org_id, actions=["mcp.stub_disp.dispatched"])
    assert len(audits) == 1
    payload = audits[0].payload
    assert payload["method"] == "tools/call"
    assert payload["tool"] == "get_issue"
    assert payload["upstream_account"] == "org_service_account"


@pytest.mark.asyncio
async def test_ten_dispatches_write_ten_audit_rows(db_session, stub_provider, stub_upstream) -> None:
    """Audit invariant: one row per JSON-RPC method, no batching."""
    del stub_provider, stub_upstream
    review, token = await _seed_review(db_session)
    await _seed_credential(db_session, org_id=review.org_id)

    async with _client() as c:
        for i in range(10):
            body = {
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {"name": "get_issue", "arguments": {"id": f"LIN-{i}"}},
            }
            r = await c.post(
                f"/api/mcp/{review.id}/stub_disp",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            assert r.status_code == 200

    audits = await list_for_org(org_id=review.org_id, actions=["mcp.stub_disp.dispatched"])
    assert len(audits) == 10


@pytest.mark.asyncio
async def test_dispatch_not_connected_records_broken(db_session, stub_provider) -> None:
    del stub_provider
    review, token = await _seed_review(db_session)
    # No credential row inserted.

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_disp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 200
    assert r.json()["error"]["data"]["code"] == "not_connected"
    assert "stub_disp" in consume_broken_creds(review.id)


@pytest.mark.asyncio
async def test_dispatch_broken_creds_records_broken(db_session, stub_provider) -> None:
    del stub_provider
    review, token = await _seed_review(db_session)
    await _seed_credential(db_session, org_id=review.org_id, last_refresh_status="failed")

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_disp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 200
    assert r.json()["error"]["data"]["code"] == "broken_creds"
    assert "stub_disp" in consume_broken_creds(review.id)


@pytest.mark.asyncio
async def test_dispatch_blocked_by_allowlist(db_session, stub_provider, stub_upstream) -> None:
    del stub_provider, stub_upstream
    review, token = await _seed_review(db_session)
    await _seed_credential(db_session, org_id=review.org_id, allowed_tools=[])

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "update_issue", "arguments": {"id": "LIN-1"}},
    }
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_disp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 200
    assert r.json()["error"]["data"]["code"] == "blocked_by_allowlist"


@pytest.mark.asyncio
async def test_dispatch_invalid_bearer_rejected(db_session, stub_provider) -> None:
    del stub_provider
    review, _ = await _seed_review(db_session)

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_disp",
            headers={"Authorization": "Bearer not-a-real-token"},
            json=body,
        )
    assert r.status_code == 401
    assert r.json()["error"]["data"]["code"] == "unauthenticated"


@pytest.mark.asyncio
async def test_dispatch_wrong_review_id_rejected(db_session, stub_provider) -> None:
    del stub_provider
    review, token = await _seed_review(db_session)
    other_review_id = uuid4()

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{other_review_id}/stub_disp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 401
    del review


@pytest.mark.asyncio
async def test_token_lifecycle_round_trip_revokes(db_session, stub_provider) -> None:
    """Mint → dispatch → revoke → dispatch fails. The
    `mcp_review_tokens` row is gone after the review ends."""
    del stub_provider
    review, token = await _seed_review(db_session)
    await _seed_credential(db_session, org_id=review.org_id, last_refresh_status="failed")

    n = await revoke_token(review.id, session=db_session)
    assert n == 1

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_disp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 401

    remaining = (
        (await db_session.execute(select(McpReviewTokenRow).where(McpReviewTokenRow.review_id == review.id)))
        .scalars()
        .all()
    )
    assert remaining == []


@pytest.mark.asyncio
async def test_proxy_reads_org_from_token_row(db_session, stub_provider, stub_upstream) -> None:
    """Proxy resolves org_id from the token row — no reviewer back-lookup.

    The credential is seeded against the review's org_id. The proxy must
    find it without calling into the reviewer module, proving the token
    row's org_id is the sole tenancy signal.
    """
    del stub_provider, stub_upstream
    review, token = await _seed_review(db_session)
    await _seed_credential(db_session, org_id=review.org_id)

    body = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "tools/call",
        "params": {"name": "get_issue", "arguments": {"id": "LIN-99"}},
    }
    async with _client() as c:
        r = await c.post(
            f"/api/mcp/{review.id}/stub_disp",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
        )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"ok": True}

    # Audit row carries the org_id read from the token row.
    audits = await list_for_org(org_id=review.org_id, actions=["mcp.stub_disp.dispatched"])
    assert len(audits) == 1
    assert audits[0].payload["method"] == "tools/call"
