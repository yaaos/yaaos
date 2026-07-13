"""OAuth authorization-server surface for `domain/mcp_server`.

Hand-rolled FastAPI routes because FastMCP's OAuthProvider.authorize() hook
receives no HTTP request object — it cannot read the session cookie, redirect
to login, or render the consent form that the browser flow requires.  FastMCP
provides the MCP protocol transport; we own the three AS endpoints.

Endpoints (all PUBLIC; bearer/session handled inside handlers):
  GET  /.well-known/oauth-authorization-server    RFC 8414 metadata document
  POST /api/mcp-server/register                   RFC 7591 dynamic client registration
  GET  /api/mcp-server/authorize                  Session-gated consent page + code issue
  POST /api/mcp-server/authorize/consent          Form submission → 302 redirect_uri?code=
  POST /api/mcp-server/token                      authorization_code + PKCE | refresh_token exchange

Public clients only: `token_endpoint_auth_method="none"`.  PKCE S256 required.
"""

from __future__ import annotations

import html
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID, uuid7

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from app.core.auth import Role, public_route
from app.core.config import get_settings
from app.core.database import session as db_session
from app.core.identity import find_session_by_hash, hash_token
from app.core.tenancy import get_member_role, list_memberships_for_user
from app.core.webserver import RouteSpec, register_routes
from app.domain.mcp_server.auth import (
    ACCESS_TOKEN_TTL,
    _hash_token,
    _verify_pkce_s256,
    mint_access_token,
    mint_refresh_token,
    rotate_refresh_token,
)
from app.domain.mcp_server.models import McpAuthCodeRow, McpOAuthClientRow

log = structlog.get_logger("domain.mcp_server.oauth")

# One-time authorization code TTL (plan: 10 minutes).
_AUTH_CODE_TTL = timedelta(minutes=10)

# /.well-known routes — registered at the root level by the composition root.
well_known_router = APIRouter()

# /api/mcp-server routes — registered via register_routes below.
router = APIRouter()


# ---------------------------------------------------------------------------
# Discovery document — RFC 8414.
# ---------------------------------------------------------------------------


@well_known_router.get(
    "/oauth-authorization-server",
    dependencies=[Depends(public_route)],
    include_in_schema=False,
    response_model=None,
)
async def oauth_server_metadata() -> JSONResponse:
    """RFC 8414 authorization server metadata.

    Clients discover the authorization and token endpoint URLs from here.
    S256 is the only supported code-challenge method (PKCE required).
    """
    settings = get_settings()
    base = settings.yaaos_public_origin.rstrip("/")
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/api/mcp-server/authorize",
            "token_endpoint": f"{base}/api/mcp-server/token",
            "registration_endpoint": f"{base}/api/mcp-server/register",
            "scopes_supported": [],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }
    )


# ---------------------------------------------------------------------------
# Dynamic client registration — RFC 7591.
# ---------------------------------------------------------------------------


class _RegisterRequest(BaseModel):
    """Registration metadata — validated hard because /register is unauthenticated."""

    client_name: str = Field(min_length=1, max_length=256)
    redirect_uris: list[str] = Field(min_length=1, max_length=5)

    @field_validator("client_name")
    @classmethod
    def _client_name_printable(cls, v: str) -> str:
        if not v.isprintable():
            raise ValueError("client_name must be printable")
        return v

    @field_validator("redirect_uris")
    @classmethod
    def _redirect_uris_allowed(cls, v: list[str]) -> list[str]:
        for uri in v:
            if len(uri) > 2048:
                raise ValueError("redirect_uri exceeds 2048 characters")
            parsed = urlsplit(uri)
            if parsed.scheme == "https":
                continue
            # Loopback carve-out for local dev clients (RFC 8252 §7.3).
            if parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"):
                continue
            raise ValueError(f"redirect_uri scheme not allowed: {parsed.scheme or '(none)'}")
        return v


@router.post("/register", dependencies=[Depends(public_route)], status_code=201, response_model=None)
async def register_client(request: Request) -> JSONResponse:
    """RFC 7591 dynamic client registration.  No prior auth required.

    Body is parsed manually so metadata violations return the RFC 7591 §3.2.2
    error shape (HTTP 400, `invalid_client_metadata`) rather than FastAPI's 422.
    Returns the minted `client_id` (UUID) which drives the rest of the flow.
    """
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "request body must be JSON"},
            status_code=400,
        )
    try:
        body = _RegisterRequest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(part) for part in first["loc"])
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": f"{loc}: {first['msg']}"},
            status_code=400,
        )

    client_id = uuid7()
    async with db_session() as s:
        row = McpOAuthClientRow(
            client_id=client_id,
            client_name=body.client_name,
            redirect_uris=body.redirect_uris,
        )
        s.add(row)
        await s.commit()

    return JSONResponse(
        {
            "client_id": str(client_id),
            "client_name": body.client_name,
            "redirect_uris": body.redirect_uris,
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Authorization endpoint — session-gated consent page.
# ---------------------------------------------------------------------------


async def _resolve_session_user(request: Request) -> UUID | None:
    """Read the yaaos_session cookie → user_id, or None if unauthenticated."""
    raw_cookie = request.cookies.get("yaaos_session")
    if not raw_cookie:
        return None
    async with db_session() as s:
        sess = await find_session_by_hash(s, hash_token(raw_cookie))
    if sess is None or sess.user_id is None:
        return None
    return sess.user_id


def _consent_html(
    *,
    client_name: str,
    orgs: list[tuple[str, str]],  # [(org_id_str, slug), ...]
    client_id: str,
    state: str | None,
    code_challenge: str,
    redirect_uri: str,
) -> str:
    """Minimal server-rendered HTML consent form.

    The SPA has no pre-auth surface; this page lives outside the SPA shell.
    Deliberately plain — no JS, no design tokens.

    Every interpolated value is HTML-escaped: `client_name` is stored verbatim
    by the unauthenticated /register endpoint and `state` is a query param —
    both are attacker-controlled (stored/reflected XSS on the yaaos origin
    otherwise). The rest are escaped defensively.
    """

    def esc(value: str) -> str:
        return html.escape(value, quote=True)

    opts = "\n".join(f'<option value="{esc(oid)}">{esc(slug)}</option>' for oid, slug in orgs)
    state_field = f'<input type="hidden" name="state" value="{esc(state)}">' if state else ""
    client_name = esc(client_name)
    client_id = esc(client_id)
    code_challenge = esc(code_challenge)
    redirect_uri = esc(redirect_uri)
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>yaaos — Authorize</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:440px;margin:4rem auto;padding:1rem}}
  h1{{font-size:1.2rem}}
  .app{{color:#7c3aed;font-weight:700}}
  label{{display:block;margin:1rem 0 .25rem;font-weight:600}}
  select,button{{width:100%;padding:.5rem;font-size:1rem;box-sizing:border-box}}
  button{{margin-top:1.5rem;background:#7c3aed;color:#fff;border:none;border-radius:4px;cursor:pointer;padding:.6rem}}
</style>
</head>
<body>
<h1>Authorize <span class="app">{client_name}</span></h1>
<p>Grant this MCP client access to yaaos on your behalf.</p>
<form method="POST" action="/api/mcp-server/authorize/consent">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  {state_field}
  <label for="org_id">Organization</label>
  <select id="org_id" name="org_id">{opts}</select>
  <button type="submit">Allow access</button>
</form>
</body>
</html>"""


@router.get("/authorize", dependencies=[Depends(public_route)], response_model=None)
async def authorize(
    request: Request,
    client_id: str = Query(...),
    response_type: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query(default="S256"),
    state: str | None = Query(default=None),
) -> HTMLResponse | RedirectResponse:
    """Authorization endpoint.

    Unauthenticated → 302 to /login?next=<this URL>.
    Authenticated → render the consent page with an org picker.
    """
    if response_type != "code":
        return HTMLResponse("<p>Unsupported response_type</p>", status_code=400)
    if code_challenge_method.upper() != "S256":
        return HTMLResponse("<p>Only S256 code_challenge_method is supported</p>", status_code=400)

    user_id = await _resolve_session_user(request)
    if user_id is None:
        settings = get_settings()
        next_url = quote(str(request.url))
        return RedirectResponse(
            f"{settings.yaaos_public_origin.rstrip('/')}/login?next={next_url}",
            status_code=302,
        )

    async with db_session() as s:
        client_row = await s.get(McpOAuthClientRow, UUID(client_id))
        if client_row is None:
            return HTMLResponse("<p>Unknown client_id</p>", status_code=400)
        if redirect_uri not in client_row.redirect_uris:
            return HTMLResponse("<p>redirect_uri not registered for this client</p>", status_code=400)

        memberships = await list_memberships_for_user(s, user_id)

    builder_orgs = [(str(m.org_id), m.slug) for m in memberships if m.role.covers(Role.BUILDER)]
    if not builder_orgs:
        return HTMLResponse(
            "<p>No organizations available — you need builder or higher role.</p>",
            status_code=403,
        )

    return HTMLResponse(
        _consent_html(
            client_name=client_row.client_name,
            orgs=builder_orgs,
            client_id=client_id,
            state=state,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
        )
    )


@router.post("/authorize/consent", dependencies=[Depends(public_route)], response_model=None)
async def authorize_consent(
    request: Request,
    client_id: str = Form(...),
    org_id: str = Form(...),
    code_challenge: str = Form(...),
    redirect_uri: str = Form(...),
    state: str | None = Form(default=None),
) -> RedirectResponse:
    """Consent form submission.

    Verifies session, client, redirect_uri, and org membership; mints a
    one-time auth code (sha256-stored, 10-minute TTL); redirects to
    `redirect_uri?code=…[&state=…]`.
    """
    user_id = await _resolve_session_user(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="session required")

    try:
        org_uuid = UUID(org_id)
        client_uuid = UUID(client_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid org_id or client_id")

    async with db_session() as s:
        client_row = await s.get(McpOAuthClientRow, client_uuid)
        if client_row is None or redirect_uri not in client_row.redirect_uris:
            raise HTTPException(status_code=400, detail="invalid client or redirect_uri")

        role = await get_member_role(s, org_id=org_uuid, user_id=user_id)
        if role is None:
            raise HTTPException(status_code=403, detail="no membership in selected org")

        raw_code = secrets.token_urlsafe(32)
        s.add(
            McpAuthCodeRow(
                code_hash=_hash_token(raw_code),
                client_id=client_uuid,
                user_id=user_id,
                org_id=org_uuid,
                code_challenge=code_challenge,
                redirect_uri=redirect_uri,
                expires_at=datetime.now(UTC) + _AUTH_CODE_TTL,
            )
        )
        await s.commit()

    params: dict[str, str] = {"code": raw_code}
    if state:
        params["state"] = state
    return RedirectResponse(redirect_uri + "?" + urlencode(params), status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint.
# ---------------------------------------------------------------------------


def _token_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/token", dependencies=[Depends(public_route)], response_model=None)
async def token_endpoint(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    code: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
) -> JSONResponse:
    """Token endpoint — authorization_code (PKCE S256) or refresh_token grant.

    Public clients only (`token_endpoint_auth_method="none"`).
    """
    if grant_type == "authorization_code":
        if not (code and code_verifier and redirect_uri):
            return _token_error(
                "invalid_request",
                "code, code_verifier, and redirect_uri required for authorization_code",
            )
        return await _exchange_auth_code(
            client_id=client_id,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
    if grant_type == "refresh_token":
        if not refresh_token:
            return _token_error("invalid_request", "refresh_token required")
        return await _exchange_refresh(client_id=client_id, refresh_token=refresh_token)
    return _token_error("unsupported_grant_type", f"Unsupported grant_type: {grant_type!r}")


async def _exchange_auth_code(
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> JSONResponse:
    try:
        client_uuid = UUID(client_id)
    except ValueError:
        return _token_error("invalid_client", "invalid client_id")

    code_hash = _hash_token(code)
    async with db_session() as s:
        code_row = (
            await s.execute(select(McpAuthCodeRow).where(McpAuthCodeRow.code_hash == code_hash))
        ).scalar_one_or_none()

        if code_row is None:
            return _token_error("invalid_grant", "code not found or already used", 401)
        if code_row.expires_at < datetime.now(UTC):
            await s.delete(code_row)
            await s.commit()
            return _token_error("invalid_grant", "authorization code expired", 401)
        if code_row.client_id != client_uuid:
            return _token_error("invalid_grant", "client_id mismatch", 401)
        if code_row.redirect_uri != redirect_uri:
            return _token_error("invalid_grant", "redirect_uri mismatch", 401)
        if not _verify_pkce_s256(code_verifier, code_row.code_challenge):
            return _token_error("invalid_grant", "code_verifier does not satisfy code_challenge", 401)

        # Consume the code (one-time use).
        await s.delete(code_row)

        access_raw = await mint_access_token(
            client_id=client_uuid,
            user_id=code_row.user_id,
            org_id=code_row.org_id,
            session=s,
        )
        refresh_raw = await mint_refresh_token(
            client_id=client_uuid,
            user_id=code_row.user_id,
            org_id=code_row.org_id,
            session=s,
        )
        await s.commit()

    return JSONResponse(
        {
            "access_token": access_raw,
            "token_type": "bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "refresh_token": refresh_raw,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _exchange_refresh(
    *,
    client_id: str,
    refresh_token: str,
) -> JSONResponse:
    try:
        client_uuid = UUID(client_id)
    except ValueError:
        return _token_error("invalid_client", "invalid client_id")

    async with db_session() as s:
        result = await rotate_refresh_token(refresh_token, client_id=client_uuid, session=s)
        if result is None:
            # No commit — validation failure must not consume the token.
            return _token_error("invalid_grant", "refresh token invalid, expired, or client mismatch", 401)
        new_access_raw, new_refresh_raw = result
        await s.commit()

    return JSONResponse(
        {
            "access_token": new_access_raw,
            "token_type": "bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "refresh_token": new_refresh_raw,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Route registration.
# ---------------------------------------------------------------------------

register_routes(
    RouteSpec(
        module_name="mcp_server",
        url_prefix="/api/mcp-server",
        router=router,
    )
)
