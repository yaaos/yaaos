"""Service-tier tests for `domain/mcp_server` FastMCP tools.

Covers:
- MCP `initialize` + `tools/call find_ticket` with a real minted bearer
  (verifier opens a real DB session); bad bearer → HTTP 401; missing ticket →
  null response; seeded ticket → found.
- Full create → attach → start → inspect loop: `create_ticket`,
  `add_attachment`, `start_run`, `get_run_overview`, `get_ticket`,
  `list_attachments`, `list_pipelines`, `list_findings`, `list_artifacts`.
- Role-floor check: BUILDER-role principal can call write tools.

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
from app.domain.pipelines import ActionStage, PipelineDefinition, create_pipeline
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


# ---------------------------------------------------------------------------
# Helpers for the new tools tests.
# ---------------------------------------------------------------------------


async def _call_tool(
    c: httpx.AsyncClient,
    name: str,
    args: dict,
    *,
    bearer: str,
    req_id: int = 99,
) -> dict:
    """POST a tools/call and return the JSON-RPC payload from the SSE response."""
    r = await c.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        headers={**_MCP_HEADERS, "Authorization": f"Bearer {bearer}"},
    )
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:400]}"
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            if payload.get("id") == req_id:
                return payload
    raise AssertionError(f"No matching JSON-RPC response in SSE body:\n{r.text[:600]}")


def _tool_result(payload: dict):
    """Extract the tool result value from a JSON-RPC success payload.

    FastMCP encodes results in two parallel fields:
    - ``content`` — list of text items; **empty** when the tool returns an empty
      collection (FastMCP does not serialise an empty list as a text item).
    - ``structuredContent["result"]`` — present and holds the typed value when
      content is empty (e.g. an empty list from list_pipelines / list_findings
      on a fresh org with no data yet).

    We use content[0]["text"] when content is non-empty (the common case) and
    fall back to structuredContent["result"] for empty-collection results.
    """
    result = payload["result"]
    assert not result.get("isError", False), f"Tool returned error: {payload}"
    content = result.get("content", [])
    if content:
        return json.loads(content[0]["text"])
    # Empty content — FastMCP puts the value in structuredContent["result"].
    sc = result.get("structuredContent")
    if sc is not None and "result" in sc:
        return sc["result"]
    raise AssertionError(f"No content and no structuredContent in tool result: {payload}")


# ---------------------------------------------------------------------------
# Full create → attach → inspect loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_attach_and_inspect_loop(db_session, mcp_client) -> None:
    """create_ticket → add_attachment → start_run → get_run_overview → inspect reads.

    Full create → attach → start → inspect flow:
    1. Seed a pipeline definition for the org (via public CRUD service).
    2. Create a ticket via MCP, attach a document.
    3. Start a pipeline run via MCP (`start_run`) — verifies the tool returns
       `{run_id}` and the run is accepted by the service layer.
    4. Fetch `get_run_overview` — verifies the RunOverview shape is returned
       (`status` ∈ {"in_flight", "paused", "terminal"}).
    5. Confirm every read tool returns the expected shape: `get_ticket`,
       `list_attachments`, `list_pipelines` (now non-empty), `list_findings`
       (empty), `list_artifacts` (empty — skill stage hasn't completed yet).
    """
    _client_id, _user_id, org_id, raw_access = await _seed_client_and_token(db_session)

    # Seed a single-stage action pipeline so start_run has a valid target.
    # Uses the public CRUD service (same approach as test_manual_kickoff_service.py).
    # ActionStage only requires action_id — no model/effort/boundary fields.
    # No drain needed: the run lands in "running" state after start_manual_run's
    # attempt_promotion; ROUTE_RUN sits in the outbox unprocessed, which is fine
    # — get_run_overview checks run.state ∈ active_states, not stage completion.
    pipeline_id = await create_pipeline(
        org_id=org_id,
        definition=PipelineDefinition(
            name=f"mcp-loop-pipe-{uuid4().hex[:6]}",
            stages=(ActionStage(action_id="mcp-test-noop"),),
        ),
        actor=Actor.system(),
        session=db_session,
    )
    await db_session.flush()

    async with mcp_client as c:
        # --- 1. create_ticket ---
        ct_payload = await _call_tool(
            c,
            "create_ticket",
            {"title": "MCP loop ticket", "repo_external_id": "acme/repo"},
            bearer=raw_access,
            req_id=10,
        )
        ct = _tool_result(ct_payload)
        ticket_id = ct["ticket_id"]
        assert ticket_id is not None
        assert ct["created"] is True

        # --- 2. add_attachment ---
        att_payload = await _call_tool(
            c,
            "add_attachment",
            {
                "ticket_id": ticket_id,
                "filename": "spec.md",
                "body": "# Requirements\nDo the thing.",
                "note": "initial spec",
            },
            bearer=raw_access,
            req_id=11,
        )
        att = _tool_result(att_payload)
        attachment_id = att["attachment_id"]
        assert attachment_id is not None
        # No frontmatter → both skill fields are None.
        assert att["produced_by_skill"] is None
        assert att["artifact_type"] is None

        # --- 3. start_run ---
        sr_payload = await _call_tool(
            c,
            "start_run",
            {"ticket_id": ticket_id, "pipeline_id": str(pipeline_id)},
            bearer=raw_access,
            req_id=12,
        )
        sr = _tool_result(sr_payload)
        run_id = sr["run_id"]
        assert run_id is not None

        # --- 4. get_run_overview ---
        # The run is promoted to "running" by start_manual_run (attempt_promotion
        # runs synchronously); get_run_overview sees run.state == "running" →
        # returns status="in_flight". No task drain required.
        ro_payload = await _call_tool(
            c, "get_run_overview", {"ticket_id": ticket_id}, bearer=raw_access, req_id=13
        )
        ro = _tool_result(ro_payload)
        assert ro is not None, "get_run_overview returned null — ticket has no run"
        assert ro["status"] in ("in_flight", "paused", "terminal"), f"unexpected status: {ro['status']}"
        # in_flight carries a `run` sub-dict; verify it holds the run_id.
        if ro["status"] == "in_flight":
            assert ro.get("run") is not None
            assert ro["run"]["id"] == run_id

        # --- 5. get_ticket ---
        gt_payload = await _call_tool(c, "get_ticket", {"ticket_id": ticket_id}, bearer=raw_access, req_id=14)
        gt = _tool_result(gt_payload)
        assert gt["id"] == ticket_id
        assert gt["title"] == "MCP loop ticket"
        # Status transitions to "running" after start_run's attempt_promotion.
        assert gt["status"] in ("pending", "running", "in_review")
        assert gt["repo_external_id"] == "acme/repo"
        assert "created_at" in gt

        # --- 6. list_attachments ---
        la_payload = await _call_tool(
            c, "list_attachments", {"ticket_id": ticket_id}, bearer=raw_access, req_id=15
        )
        la = _tool_result(la_payload)
        assert isinstance(la, list)
        assert len(la) == 1
        assert la[0]["id"] == attachment_id
        assert la[0]["filename"] == "spec.md"
        assert la[0]["note"] == "initial spec"

        # --- 7. list_pipelines (one seeded pipeline) ---
        lp_payload = await _call_tool(c, "list_pipelines", {}, bearer=raw_access, req_id=16)
        lp = _tool_result(lp_payload)
        assert isinstance(lp, list)
        assert len(lp) == 1
        assert lp[0]["id"] == str(pipeline_id)
        assert "name" in lp[0]
        assert "description" in lp[0]

        # --- 8. list_findings (none yet) ---
        lf_payload = await _call_tool(
            c, "list_findings", {"ticket_id": ticket_id}, bearer=raw_access, req_id=17
        )
        lf = _tool_result(lf_payload)
        assert lf == []

        # --- 9. list_artifacts (none yet — skill stage has not completed) ---
        lar_payload = await _call_tool(
            c, "list_artifacts", {"ticket_id": ticket_id}, bearer=raw_access, req_id=18
        )
        lar = _tool_result(lar_payload)
        assert lar == []


# ---------------------------------------------------------------------------
# Role floor — write tools require builder+.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ticket_role_floor_builder_can_write(db_session, mcp_client) -> None:
    """BUILDER-role principal can call write tools (role floor is satisfied).

    All current org roles are BUILDER+ (OWNER, ADMIN, BUILDER); the role check
    runs and passes for the minimum allowed role.  This test exercises the
    `_require_writer_role` code path end-to-end through the FastMCP stack.
    """
    _client_id, _user_id, _org_id, raw_access = await _seed_client_and_token(db_session, role=Role.BUILDER)

    async with mcp_client as c:
        payload = await _call_tool(
            c,
            "create_ticket",
            {"title": "Role floor test", "repo_external_id": "test/repo"},
            bearer=raw_access,
            req_id=50,
        )
    result = _tool_result(payload)
    assert result["ticket_id"] is not None
    assert result["created"] is True
