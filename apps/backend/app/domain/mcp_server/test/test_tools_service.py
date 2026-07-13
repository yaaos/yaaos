"""Service-tier tests for `domain/mcp_server` FastMCP tools.

Covers: MCP `initialize` + `tools/call find_ticket` with a real minted bearer
(verifier opens a real DB session); bad bearer → HTTP 401; missing ticket →
null response; seeded ticket → found.

All tests use real Postgres via `db_session` (transactional rollback).
The FastMCP http_app lifespan is started via its router.lifespan_context
so the StreamableHTTPSessionManager task group initialises before requests.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.core.audit_log import Actor, ActorKind
from app.core.auth import Role
from app.core.identity import create_user
from app.core.tenancy import create_membership
from app.domain.mcp_server.auth import mint_access_token
from app.domain.mcp_server.models import McpOAuthClientRow
from app.domain.mcp_server.tools import mcp
from app.domain.orgs import insert_org
from app.domain.tickets import create_from_manual

# Every test in this file is a service test.
pytestmark = pytest.mark.service

# ---------------------------------------------------------------------------
# Shared headers for MCP requests.
# ---------------------------------------------------------------------------

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def mcp_http_app():
    """FastMCP ASGI sub-app with its lifespan running.

    Module-scoped so the anyio task group lives in the session event loop
    (the same loop as session-scoped fixtures like _migrated_schema), avoiding
    "Future attached to a different loop" contamination in subsequent tests.

    Runs the StreamableHTTPSessionManager task group in a dedicated asyncio
    task so that anyio's CancelScope is both created and exited in the same
    task — avoiding the "different task" RuntimeError on teardown.
    """
    http_app = mcp.http_app(path="/", stateless_http=True)

    lifespan_started: asyncio.Event = asyncio.Event()
    lifespan_exit: asyncio.Event = asyncio.Event()

    async def _run_lifespan() -> None:
        async with http_app.router.lifespan_context(http_app):
            lifespan_started.set()
            await lifespan_exit.wait()

    task = asyncio.create_task(_run_lifespan())
    await lifespan_started.wait()
    try:
        yield http_app
    finally:
        lifespan_exit.set()
        await task


@pytest.fixture
def mcp_client(mcp_http_app):
    """httpx.AsyncClient backed by the FastMCP ASGI app (no network)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mcp_http_app),
        base_url="http://test",
    )


async def _seed_client_and_token(
    db_session,
    *,
    role: Role = Role.BUILDER,
) -> tuple[str, uuid4, uuid4, str]:
    """Create an org, user, membership, and mint a valid access token.

    Returns (client_id_str, user_id, org_id, raw_access_token).
    """
    org = await insert_org(db_session, slug=f"mcp-tools-{uuid4().hex[:8]}")
    user = await create_user(db_session)
    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.org_id,
        role=role,
        handle=f"u-{uuid4().hex[:6]}",
    )
    client_id = uuid4()
    db_session.add(
        McpOAuthClientRow(
            client_id=client_id,
            client_name="test-tool-client",
            redirect_uris=["http://localhost/cb"],
        )
    )
    await db_session.flush()
    raw_access = await mint_access_token(
        client_id=client_id,
        user_id=user.id,
        org_id=org.org_id,
        session=db_session,
    )
    await db_session.commit()
    return str(client_id), user.id, org.org_id, raw_access


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_with_valid_bearer(db_session, mcp_client) -> None:
    """MCP `initialize` succeeds with a valid access token."""
    _client_id, _user_id, _org_id, raw_access = await _seed_client_and_token(db_session)

    async with mcp_client as c:
        r = await c.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {raw_access}"},
        )
    assert r.status_code == 200
    # Response is SSE; ensure the MCP initialize result is present.
    assert "protocolVersion" in r.text


@pytest.mark.asyncio
async def test_initialize_bad_bearer_returns_401(mcp_client) -> None:
    """MCP request with an invalid bearer → HTTP 401."""
    async with mcp_client as c:
        r = await c.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            headers={**_MCP_HEADERS, "Authorization": "Bearer totally-invalid-token"},
        )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# find_ticket tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_ticket_returns_null_for_unknown_branch(db_session, mcp_client) -> None:
    """find_ticket with no matching ticket → all-null dict."""
    _client_id, _user_id, _org_id, raw_access = await _seed_client_and_token(db_session)

    async with mcp_client as c:
        r = await c.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "find_ticket",
                    "arguments": {"branch_name": "nonexistent-branch-xyz"},
                },
            },
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {raw_access}"},
        )
    assert r.status_code == 200
    # The response body is SSE; extract the JSON-RPC result from the event data.
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            if payload.get("id") == 2:
                result = payload["result"]
                content = result["content"]
                text = content[0]["text"]
                data = json.loads(text)
                assert data["ticket_id"] is None
                assert data["title"] is None
                assert data["status"] is None
                break


@pytest.mark.asyncio
async def test_find_ticket_bad_bearer_returns_401(mcp_client) -> None:
    """tools/call find_ticket with a bad bearer → HTTP 401 (FastMCP auth middleware)."""
    async with mcp_client as c:
        r = await c.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "find_ticket",
                    "arguments": {"branch_name": "some-branch"},
                },
            },
            headers={**_MCP_HEADERS, "Authorization": "Bearer bad-bearer-value"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_find_ticket_with_seeded_ticket(db_session, mcp_client) -> None:
    """find_ticket finds a ticket created via the tickets service layer."""
    _client_id, user_id, org_id, raw_access = await _seed_client_and_token(db_session)

    # Create a ticket via the public domain service so it appears in DB.
    # create_from_manual calls resolve_plugin_id_for_repo which returns ""
    # in test mode (no VCS plugin registered); that is fine — plugin_id=""
    # is stored on the ticket row and doesn't affect the branch lookup.
    branch = f"feature/mcp-test-{uuid4().hex[:8]}"
    actor = Actor(kind=ActorKind.USER, user_id=user_id)
    await create_from_manual(
        org_id=org_id,
        title="Test ticket for MCP find_ticket",
        repo_external_id="test-repo",
        actor=actor,
        session=db_session,
        branch_name=branch,
    )
    await db_session.commit()

    async with mcp_client as c:
        r = await c.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "find_ticket",
                    "arguments": {"branch_name": branch},
                },
            },
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {raw_access}"},
        )
    assert r.status_code == 200

    found = False
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            if payload.get("id") == 4:
                result = payload["result"]
                text = result["content"][0]["text"]
                data = json.loads(text)
                assert data["ticket_id"] is not None
                assert data["title"] == "Test ticket for MCP find_ticket"
                assert data["status"] == "pending"
                found = True
                break
    assert found, "No matching JSON-RPC response found in SSE body"
