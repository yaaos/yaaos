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
from sqlalchemy import select

from app.core.audit_log.models import AuditEntryRow
from app.core.auth import AuthMiddleware
from app.core.oauth import ProviderConfig
from app.core.secrets import encrypt
from app.core.webserver.registry import _specs
from app.domain.identity import repository as identity_repo
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.types import _REGISTRY
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
from app.domain.pull_requests.models import PullRequestRow
from app.domain.reviewer.models import ReviewRow
from app.domain.tickets.models import TicketRow

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
        client_secret="csecret",
        scope_separator=" ",
        default_scopes=("read",),
        known_read_tools=("get_issue", "search_issues"),
        known_write_tools=("update_issue", "create_comment"),
    )


@dataclass
class _StubProvider:
    provider_id: str = "stub_disp"
    config: ProviderConfig = field(default_factory=_config)

    async def validate(self, access_token: str) -> bool:
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
    spec = _specs["mcp"]
    app.include_router(spec.router, prefix=spec.url_prefix or "/api/mcp")
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _seed_review(db_session) -> tuple[ReviewRow, str]:
    org = await orgs_repo.insert_org(db_session, slug=f"mcp-disp-{uuid4().hex[:8]}")
    await identity_repo.insert_user(db_session, display_name="U")
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
    row = McpCredentialRow(
        org_id=org_id,
        provider="stub_disp",
        encrypted_access_token=encrypt("upstream-access").decode(),
        encrypted_refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
        scopes=["read"],
        allowed_tools=allowed_tools or [],
        enabled=enabled,
        upstream_identity="stub-bot",
        last_refresh_status=last_refresh_status,
        last_validated_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
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

    audits = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.org_id == review.org_id,
                    AuditEntryRow.kind == "mcp.stub_disp.dispatched",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    payload = audits[0].payload
    assert payload["method"] == "tools/call"
    assert payload["tool"] == "get_issue"
    assert payload["upstream_account"] == "org_service_account"


@pytest.mark.asyncio
async def test_ten_dispatches_write_ten_audit_rows(db_session, stub_provider, stub_upstream) -> None:
    """Phase 8 audit invariant: one row per JSON-RPC method, no batching."""
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

    audits = (
        (
            await db_session.execute(
                select(AuditEntryRow).where(
                    AuditEntryRow.org_id == review.org_id,
                    AuditEntryRow.kind == "mcp.stub_disp.dispatched",
                )
            )
        )
        .scalars()
        .all()
    )
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
    """Mint → dispatch → revoke → dispatch fails. Phase 5 spec item:
    `mcp_review_tokens` row is gone after the review ends."""
    del stub_provider
    review, token = await _seed_review(db_session)
    await _seed_credential(db_session, org_id=review.org_id, last_refresh_status="failed")

    n = await revoke_token(review.id)
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
