"""fake-notion FastAPI service.

Implements just enough of Notion's OAuth + hosted-MCP surface to drive
yaaos end-to-end. MCP Streamable HTTP at `POST /mcp`. Notion's OAuth
uses HTTP Basic for `/oauth/token` rather than form-encoded client creds;
yaaos's IntegrationProvider config encodes that quirk so the upstream
provider plugin layer is the only place that knows.

State is in-memory; `/__test/reset` restores seeds between test runs.
"""

from __future__ import annotations

import base64
import secrets
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import seeds
from app.test_secrets import CLIENT_ID, CLIENT_SECRET

app = FastAPI(title="fake-notion")


_ACCESS_TOKENS: dict[str, dict[str, Any]] = {}
# Notion's API tokens do not expire and are not refreshed; yaaos still
# stores a refresh-token column for shape parity. We honour the same shape
# the IntegrationProvider expects but treat refresh as a no-op rotation.
_REFRESH_TOKENS: dict[str, str] = {}
_PENDING_CODES: dict[str, dict[str, Any]] = {}


# ── OAuth ────────────────────────────────────────────────────────────────────


@app.get("/v1/oauth/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    state: str,
    owner: str = "user",
    response_type: str = "code",
) -> RedirectResponse:
    del owner, response_type
    if client_id != CLIENT_ID:
        raise HTTPException(status_code=400, detail={"error": "invalid_client"})
    code = secrets.token_urlsafe(16)
    _PENDING_CODES[code] = {"redirect_uri": redirect_uri}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}code={code}&state={state}", status_code=303
    )


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    except Exception:  # noqa: BLE001
        return None
    if ":" not in decoded:
        return None
    cid, secret = decoded.split(":", 1)
    return cid, secret


@app.post("/v1/oauth/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    refresh_token: str | None = Form(None),
) -> JSONResponse:
    """Notion uses HTTP Basic for client auth on the token endpoint."""
    creds = _parse_basic_auth(request.headers.get("authorization"))
    if creds is None or creds != (CLIENT_ID, CLIENT_SECRET):
        raise HTTPException(status_code=401, detail={"error": "invalid_client"})

    if grant_type == "authorization_code":
        pending = _PENDING_CODES.pop(code or "", None)
        if pending is None:
            raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
        if redirect_uri and redirect_uri != pending["redirect_uri"]:
            raise HTTPException(status_code=400, detail={"error": "redirect_uri_mismatch"})
    elif grant_type == "refresh_token":
        if refresh_token is None or _REFRESH_TOKENS.get(refresh_token) != "valid":
            raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
    else:
        raise HTTPException(status_code=400, detail={"error": "unsupported_grant_type"})

    access = secrets.token_urlsafe(24)
    refresh = secrets.token_urlsafe(24)
    _ACCESS_TOKENS[access] = {"refresh_token": refresh}
    _REFRESH_TOKENS[refresh] = "valid"
    return JSONResponse(
        {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            # Notion's real tokens have no expiry; we still return a generous
            # value so yaaos's refresh path round-trips successfully.
            "expires_in": 365 * 24 * 3600,
            "workspace_name": "Fake Notion Workspace",
            "workspace_id": "ws-1",
            "owner": {"type": "user", "user": {"id": "user-1", "name": "Service Account"}},
        }
    )


@app.get("/v1/users/me")
async def me(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") not in _ACCESS_TOKENS:
        raise HTTPException(status_code=401, detail={"error": "unauthenticated"})
    return JSONResponse({"object": "user", "id": "user-1", "name": "Service Account"})


# ── MCP ──────────────────────────────────────────────────────────────────────


_TOOLS = [
    {"name": "search", "description": "Search pages by substring", "kind": "read"},
    {"name": "query_database", "description": "List pages in a database", "kind": "read"},
    {"name": "retrieve_page", "description": "Fetch one page", "kind": "read"},
    {"name": "retrieve_block", "description": "Fetch one block", "kind": "read"},
    {"name": "update_page", "description": "Patch page fields", "kind": "write"},
    {"name": "create_comment", "description": "Add a comment to a page", "kind": "write"},
]


def _jsonrpc_ok(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _jsonrpc_err(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


@app.post("/mcp")
async def mcp(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") not in _ACCESS_TOKENS:
        raise HTTPException(status_code=401, detail={"error": "unauthenticated"})

    body = await request.json()
    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "tools/list":
        return JSONResponse(_jsonrpc_ok(rpc_id, {"tools": _TOOLS}))

    if method == "tools/call":
        tool = params.get("name")
        args = params.get("arguments") or {}
        try:
            return JSONResponse(_jsonrpc_ok(rpc_id, _dispatch_tool(tool, args)))
        except KeyError as exc:
            return JSONResponse(_jsonrpc_err(rpc_id, -32602, f"not found: {exc}"))
        except ValueError as exc:
            return JSONResponse(_jsonrpc_err(rpc_id, -32602, str(exc)))

    return JSONResponse(_jsonrpc_err(rpc_id, -32601, f"method not found: {method}"))


def _dispatch_tool(tool: str | None, args: dict[str, Any]) -> Any:
    if tool == "search":
        return seeds.search(args.get("query", ""))
    if tool == "query_database":
        return seeds.query_database(args.get("database_id", ""))
    if tool == "retrieve_page":
        page = seeds.retrieve_page(args.get("page_id", ""))
        if page is None:
            raise KeyError(args.get("page_id", ""))
        return page
    if tool == "retrieve_block":
        block = seeds.retrieve_block(args.get("block_id", ""))
        if block is None:
            raise KeyError(args.get("block_id", ""))
        return block
    if tool == "update_page":
        return seeds.update_page(args.get("page_id", ""), args.get("fields") or {})
    if tool == "create_comment":
        return seeds.create_comment(args.get("page_id", ""), args.get("body", ""))
    raise ValueError(f"unknown tool: {tool}")


# ── Test helpers ─────────────────────────────────────────────────────────────


@app.post("/__test/reset")
async def reset() -> dict[str, str]:
    seeds.reset()
    _ACCESS_TOKENS.clear()
    _REFRESH_TOKENS.clear()
    _PENDING_CODES.clear()
    return {"ok": "reset"}
