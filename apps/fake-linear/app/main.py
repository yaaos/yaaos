"""fake-linear FastAPI service.

Implements just enough of the Linear OAuth + hosted-MCP surface to drive
the yaaos test suite end-to-end without registering a real Linear OAuth
app. The MCP endpoint accepts JSON-RPC 2.0 over POST (Streamable HTTP) at
`/sse`; SSE upgrade isn't exercised by the yaaos proxy today, so we keep
the response model JSON-only.

State is in-memory; the `/__test/reset` hook restores seed data between
test runs (yaaos's e2e harness calls it as part of its reset chain).
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import seeds
from app.test_secrets import CLIENT_ID, CLIENT_SECRET

app = FastAPI(title="fake-linear")


# In-memory token stores. Real Linear rotates refresh tokens; we mirror that
# so refresh tests prove yaaos handles rotation correctly.
_ACCESS_TOKENS: dict[str, dict[str, Any]] = {}  # token -> {scope, refresh_token}
_REFRESH_TOKENS: dict[str, str] = {}  # refresh -> "valid" or absent


# ── OAuth ────────────────────────────────────────────────────────────────────


@app.get("/oauth/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str = "read",
    response_type: str = "code",
) -> RedirectResponse:
    """Auto-grants — no UI. Mints a one-shot authorization code bound to the
    state so the test harness can drive the dance without a browser."""
    del response_type
    if client_id != CLIENT_ID:
        raise HTTPException(status_code=400, detail={"error": "invalid_client"})
    code = secrets.token_urlsafe(16)
    _PENDING_CODES[code] = {"scope": scope, "redirect_uri": redirect_uri}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}code={code}&state={state}", status_code=303
    )


_PENDING_CODES: dict[str, dict[str, Any]] = {}


@app.post("/oauth/token")
async def token(
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
    refresh_token: str | None = Form(None),
) -> JSONResponse:
    """Both initial exchange and refresh — Linear uses the same endpoint
    distinguished by `grant_type`."""
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        raise HTTPException(status_code=400, detail={"error": "invalid_client"})

    if grant_type == "authorization_code":
        pending = _PENDING_CODES.pop(code or "", None)
        if pending is None:
            raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
        if redirect_uri and redirect_uri != pending["redirect_uri"]:
            raise HTTPException(status_code=400, detail={"error": "redirect_uri_mismatch"})
        scope = pending["scope"]
    elif grant_type == "refresh_token":
        if refresh_token is None or _REFRESH_TOKENS.get(refresh_token) != "valid":
            raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
        # Linear rotates refresh tokens — invalidate the old one.
        del _REFRESH_TOKENS[refresh_token]
        scope = "read"
    else:
        raise HTTPException(status_code=400, detail={"error": "unsupported_grant_type"})

    access = secrets.token_urlsafe(24)
    refresh = secrets.token_urlsafe(24)
    _ACCESS_TOKENS[access] = {"scope": scope, "refresh_token": refresh}
    _REFRESH_TOKENS[refresh] = "valid"
    return JSONResponse(
        {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": scope,
        }
    )


@app.get("/api/me")
async def me(request: Request) -> JSONResponse:
    """Lightweight identity probe — used by the IntegrationProvider validator."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") not in _ACCESS_TOKENS:
        raise HTTPException(status_code=401, detail={"error": "unauthenticated"})
    return JSONResponse({"id": "user-1", "email": "service-account@example.com"})


# ── MCP — Streamable HTTP at /sse ────────────────────────────────────────────


_TOOLS = [
    {"name": "get_issue", "description": "Fetch one Linear issue by id", "kind": "read"},
    {"name": "search_issues", "description": "Substring search", "kind": "read"},
    {"name": "list_projects", "description": "List projects", "kind": "read"},
    {"name": "list_cycles", "description": "List cycles", "kind": "read"},
    {"name": "update_issue", "description": "Patch issue fields", "kind": "write"},
    {"name": "create_comment", "description": "Add a comment to an issue", "kind": "write"},
]


def _jsonrpc_ok(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _jsonrpc_err(rpc_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


@app.post("/sse")
async def mcp(request: Request) -> JSONResponse:
    """Streamable-HTTP MCP endpoint. Returns plain JSON-RPC; SSE upgrade
    isn't required by the yaaos proxy. Bearer required."""
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
    if tool == "get_issue":
        issue = seeds.get_issue(args.get("id", ""))
        if issue is None:
            raise KeyError(args.get("id", ""))
        return issue
    if tool == "search_issues":
        return seeds.search_issues(args.get("query", ""))
    if tool == "list_projects":
        return seeds.list_projects()
    if tool == "list_cycles":
        return seeds.list_cycles()
    if tool == "update_issue":
        return seeds.update_issue(args.get("id", ""), args.get("fields") or {})
    if tool == "create_comment":
        return seeds.create_comment(args.get("issue_id", ""), args.get("body", ""))
    raise ValueError(f"unknown tool: {tool}")


# ── Test helpers ─────────────────────────────────────────────────────────────


@app.post("/__test/reset")
async def reset() -> dict[str, str]:
    seeds.reset()
    _ACCESS_TOKENS.clear()
    _REFRESH_TOKENS.clear()
    _PENDING_CODES.clear()
    return {"ok": "reset"}
