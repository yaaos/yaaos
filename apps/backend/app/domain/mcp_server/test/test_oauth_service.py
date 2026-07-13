"""Service-tier tests for `domain/mcp_server` OAuth flow.

Covers: register → authorize (seeded session; consent approve) →
code exchange with correct / wrong PKCE verifier → token works /
`invalid_grant`; refresh rotation (old refresh becomes invalid);
expired access token → 401; sweep deletes expired rows.

All tests use real Postgres via `db_session` (transactional rollback).
HTTP routes exercised via `httpx.ASGITransport` (in-process, no network).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select

from app.core.auth import AuthMiddleware, Role
from app.core.identity import create_user, mint_session
from app.core.tenancy import create_membership
from app.core.webserver import mount_specs
from app.domain.mcp_server.auth import (
    McpAuthError,
    McpPrincipal,
    _hash_token,
    _sweep_expired_tokens,
    authenticate,
    mcp_server_token_sweep,
    mint_access_token,
    mint_refresh_token,
    revoke_tokens_for_user,
)
from app.domain.mcp_server.models import McpAccessTokenRow, McpRefreshTokenRow
from app.domain.orgs import insert_org

# Every test in this file is a service test.
pytestmark = pytest.mark.service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge_s256) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    mount_specs(app, only={"mcp_server"})
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test")


async def _seed_user_org_session(
    db_session,
    *,
    role: Role = Role.BUILDER,
) -> tuple[UUID, UUID, str]:
    """Create a user + org + membership + session cookie.  Returns (user_id, org_id, raw_cookie)."""
    org = await insert_org(db_session, slug=f"mcp-svc-{uuid4().hex[:8]}")
    user = await create_user(db_session)
    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.org_id,
        role=role,
        handle=f"u-{uuid4().hex[:6]}",
    )
    created = await mint_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.commit()
    return user.id, org.org_id, created.raw_token


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_returns_client_id() -> None:
    """POST /api/mcp-server/register creates a client and returns client_id."""
    async with _client() as c:
        r = await c.post(
            "/api/mcp-server/register",
            json={
                "client_name": "test-client",
                "redirect_uris": ["http://localhost:3000/callback"],
            },
        )
    assert r.status_code == 201
    data = r.json()
    assert "client_id" in data
    UUID(data["client_id"])  # must be a valid UUID
    assert data["token_endpoint_auth_method"] == "none"


@pytest.mark.asyncio
async def test_register_rejects_empty_redirect_uris() -> None:
    async with _client() as c:
        r = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "bad", "redirect_uris": []},
        )
    assert r.status_code == 400
    assert "redirect_uris" in r.json()["error_description"]


@pytest.mark.asyncio
async def test_register_rejects_javascript_redirect_uri() -> None:
    """Only https:// (plus http://localhost / http://127.0.0.1) schemes are allowed."""
    async with _client() as c:
        r = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "bad", "redirect_uris": ["javascript:alert(1)"]},
        )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


@pytest.mark.asyncio
async def test_register_rejects_http_non_localhost_and_data_uris() -> None:
    async with _client() as c:
        for bad_uri in ("http://evil.example/cb", "data:text/html,x", "ftp://x/cb"):
            r = await c.post(
                "/api/mcp-server/register",
                json={"client_name": "bad", "redirect_uris": [bad_uri]},
            )
            assert r.status_code == 400, bad_uri
            assert r.json()["error"] == "invalid_client_metadata"


@pytest.mark.asyncio
async def test_register_accepts_https_and_localhost_uris() -> None:
    async with _client() as c:
        r = await c.post(
            "/api/mcp-server/register",
            json={
                "client_name": "good",
                "redirect_uris": [
                    "https://example.com/callback",
                    "http://localhost:3000/callback",
                    "http://127.0.0.1:8123/cb",
                ],
            },
        )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_register_caps_client_name_and_uri_counts() -> None:
    async with _client() as c:
        too_long_name = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "x" * 257, "redirect_uris": ["https://example.com/cb"]},
        )
        unprintable_name = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "bad\x00name", "redirect_uris": ["https://example.com/cb"]},
        )
        too_many_uris = await c.post(
            "/api/mcp-server/register",
            json={
                "client_name": "ok",
                "redirect_uris": [f"https://example.com/cb{i}" for i in range(6)],
            },
        )
        too_long_uri = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "ok", "redirect_uris": ["https://example.com/" + "a" * 2048]},
        )
    for r in (too_long_name, unprintable_name, too_many_uris, too_long_uri):
        assert r.status_code == 400
        assert r.json()["error"] == "invalid_client_metadata"


# ---------------------------------------------------------------------------
# Authorization code flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_redirects_unauthenticated_to_login(db_session) -> None:
    """GET /api/mcp-server/authorize without a session cookie → 302 to /login."""
    async with _client() as c:
        # Register a client first.
        reg = await c.post(
            "/api/mcp-server/register",
            json={
                "client_name": "test-client",
                "redirect_uris": ["http://localhost:3000/callback"],
            },
        )
        client_id = reg.json()["client_id"]
        _, challenge = _pkce_pair()

        r = await c.get(
            "/api/mcp-server/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": "http://localhost:3000/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


@pytest.mark.asyncio
async def test_authorize_renders_consent_page_for_authenticated_user(db_session) -> None:
    """GET /api/mcp-server/authorize with valid session → 200 HTML consent page."""
    _user_id, _org_id, raw_cookie = await _seed_user_org_session(db_session)
    _, challenge = _pkce_pair()

    async with _client() as c:
        reg = await c.post(
            "/api/mcp-server/register",
            json={
                "client_name": "test-client",
                "redirect_uris": ["http://localhost:3000/callback"],
            },
        )
        client_id = reg.json()["client_id"]

        r = await c.get(
            "/api/mcp-server/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": "http://localhost:3000/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            cookies={"yaaos_session": raw_cookie},
        )
    assert r.status_code == 200
    assert b"Allow access" in r.content


@pytest.mark.asyncio
async def test_consent_page_escapes_user_supplied_values(db_session) -> None:
    """`client_name` (stored verbatim by the unauthenticated /register) and
    `state` (query param) must be HTML-escaped in the consent page — otherwise
    stored/reflected XSS on the yaaos origin."""
    _user_id, _org_id, raw_cookie = await _seed_user_org_session(db_session)
    _, challenge = _pkce_pair()

    async with _client() as c:
        reg = await c.post(
            "/api/mcp-server/register",
            json={
                "client_name": "<script>alert(1)</script>",
                "redirect_uris": ["http://localhost:3000/callback"],
            },
        )
        client_id = reg.json()["client_id"]

        r = await c.get(
            "/api/mcp-server/authorize",
            params={
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": "http://localhost:3000/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": '" onmouseover="x',
            },
            cookies={"yaaos_session": raw_cookie},
        )
    assert r.status_code == 200
    # client_name renders escaped — no raw <script> in the response body.
    assert b"<script>alert(1)</script>" not in r.content
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in r.content
    # state cannot break out of the hidden input's value attribute.
    assert b'value="" onmouseover=' not in r.content
    assert b"&quot; onmouseover=&quot;x" in r.content


# ---------------------------------------------------------------------------
# Consent → code → token (happy path)
# ---------------------------------------------------------------------------


async def _full_flow(
    db_session,
    *,
    role: Role = Role.BUILDER,
) -> tuple[str, str, str, str, UUID, UUID]:
    """Register → authorize/consent → token exchange.

    Returns (access_raw, refresh_raw, verifier, client_id, user_id, org_id).
    """
    user_id, org_id, raw_cookie = await _seed_user_org_session(db_session, role=role)
    verifier, challenge = _pkce_pair()

    async with _client() as c:
        # 1. Register.
        reg = await c.post(
            "/api/mcp-server/register",
            json={
                "client_name": "test-client",
                "redirect_uris": ["http://localhost:3000/callback"],
            },
        )
        client_id = reg.json()["client_id"]

        # 2. Consent.
        consent = await c.post(
            "/api/mcp-server/authorize/consent",
            data={
                "client_id": client_id,
                "org_id": str(org_id),
                "code_challenge": challenge,
                "redirect_uri": "http://localhost:3000/callback",
            },
            cookies={"yaaos_session": raw_cookie},
            follow_redirects=False,
        )
    assert consent.status_code == 302
    location = consent.headers["location"]
    code = dict(pair.split("=", 1) for pair in location.split("?", 1)[1].split("&"))["code"]

    async with _client() as c:
        # 3. Token exchange.
        tok = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "http://localhost:3000/callback",
            },
        )
    assert tok.status_code == 200
    tok_data = tok.json()
    return tok_data["access_token"], tok_data["refresh_token"], verifier, client_id, user_id, org_id


@pytest.mark.asyncio
async def test_full_flow_mints_access_and_refresh_tokens(db_session) -> None:
    """Happy path: register → consent → token exchange returns access + refresh tokens."""
    access_raw, refresh_raw, _, _, _, _ = await _full_flow(db_session)
    assert len(access_raw) > 20
    assert len(refresh_raw) > 20


@pytest.mark.asyncio
async def test_wrong_pkce_verifier_returns_invalid_grant(db_session) -> None:
    """Token exchange with wrong code_verifier → 401 invalid_grant."""
    _user_id, org_id, raw_cookie = await _seed_user_org_session(db_session)
    _, challenge = _pkce_pair()

    async with _client() as c:
        reg = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "t", "redirect_uris": ["http://localhost:3000/callback"]},
        )
        client_id = reg.json()["client_id"]

        consent = await c.post(
            "/api/mcp-server/authorize/consent",
            data={
                "client_id": client_id,
                "org_id": str(org_id),
                "code_challenge": challenge,
                "redirect_uri": "http://localhost:3000/callback",
            },
            cookies={"yaaos_session": raw_cookie},
            follow_redirects=False,
        )
    code = dict(pair.split("=", 1) for pair in consent.headers["location"].split("?", 1)[1].split("&"))[
        "code"
    ]

    async with _client() as c:
        bad_tok = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": "this-is-wrong",
                "redirect_uri": "http://localhost:3000/callback",
            },
        )
    assert bad_tok.status_code == 401
    assert bad_tok.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_code_cannot_be_reused(db_session) -> None:
    """Authorization code is single-use; second exchange → 401 invalid_grant."""
    _user_id, org_id, raw_cookie = await _seed_user_org_session(db_session)
    verifier, challenge = _pkce_pair()

    async with _client() as c:
        reg = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "t", "redirect_uris": ["http://localhost:3000/callback"]},
        )
        client_id = reg.json()["client_id"]

        consent = await c.post(
            "/api/mcp-server/authorize/consent",
            data={
                "client_id": client_id,
                "org_id": str(org_id),
                "code_challenge": challenge,
                "redirect_uri": "http://localhost:3000/callback",
            },
            cookies={"yaaos_session": raw_cookie},
            follow_redirects=False,
        )
    code = dict(pair.split("=", 1) for pair in consent.headers["location"].split("?", 1)[1].split("&"))[
        "code"
    ]

    async with _client() as c:
        tok1 = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "http://localhost:3000/callback",
            },
        )
        tok2 = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": "http://localhost:3000/callback",
            },
        )
    assert tok1.status_code == 200
    assert tok2.status_code == 401
    assert tok2.json()["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Authenticate — valid / expired / revoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_bearer_authenticates(db_session) -> None:
    """A freshly minted access token resolves to the correct McpPrincipal."""
    access_raw, _, _, _, user_id, org_id = await _full_flow(db_session)
    async with db_session.begin_nested():
        principal = await authenticate(SecretStr(access_raw), session=db_session)
    assert isinstance(principal, McpPrincipal)
    assert principal.user_id == user_id
    assert principal.org_id == org_id


@pytest.mark.asyncio
async def test_expired_bearer_raises_mcp_auth_error(db_session) -> None:
    """An access token past its TTL → `McpAuthError`."""
    org = await insert_org(db_session, slug=f"mcp-exp-{uuid4().hex[:8]}")
    user = await create_user(db_session)
    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.org_id,
        role=Role.BUILDER,
        handle="u",
    )
    # Register a client manually.
    from uuid import uuid4 as _uuid4  # noqa: PLC0415

    from app.domain.mcp_server.models import McpOAuthClientRow  # noqa: PLC0415

    client_id = _uuid4()
    db_session.add(
        McpOAuthClientRow(
            client_id=client_id,
            client_name="t",
            redirect_uris=["http://localhost/cb"],
        )
    )
    await db_session.flush()
    raw = await mint_access_token(client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session)
    # Backdate the token to expired.
    tok_row = (
        await db_session.execute(
            select(McpAccessTokenRow).where(McpAccessTokenRow.token_hash == _hash_token(raw))
        )
    ).scalar_one()
    tok_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    with pytest.raises(McpAuthError, match="expired"):
        await authenticate(SecretStr(raw), session=db_session)


@pytest.mark.asyncio
async def test_unknown_bearer_raises_mcp_auth_error(db_session) -> None:
    """A random string → `McpAuthError` (invalid bearer)."""
    with pytest.raises(McpAuthError, match="invalid"):
        await authenticate(SecretStr("never-issued-token"), session=db_session)


# ---------------------------------------------------------------------------
# Refresh token rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_rotation_issues_new_token_pair(db_session) -> None:
    """Refresh token exchange mints new access + refresh tokens; old refresh dies."""
    access_raw, refresh_raw, _, client_id, _user_id, _org_id = await _full_flow(db_session)

    async with _client() as c:
        rot = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_raw,
            },
        )
    assert rot.status_code == 200
    new_data = rot.json()
    assert new_data["access_token"] != access_raw
    assert new_data["refresh_token"] != refresh_raw
    assert len(new_data["access_token"]) > 20
    assert len(new_data["refresh_token"]) > 20


@pytest.mark.asyncio
async def test_old_refresh_is_invalid_after_rotation(db_session) -> None:
    """After refresh rotation, the original refresh token is single-use."""
    _, refresh_raw, _, client_id, _, _ = await _full_flow(db_session)

    async with _client() as c:
        rot1 = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_raw,
            },
        )
        rot2 = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_raw,
            },
        )
    assert rot1.status_code == 200
    assert rot2.status_code == 401
    assert rot2.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_refresh_with_mismatched_client_is_not_consumed(db_session) -> None:
    """RFC 6749 §5.2 — a refresh attempt that fails client validation must NOT
    consume the token: the legitimate holder's refresh still works afterwards."""
    _, refresh_raw, _, client_id, _, _ = await _full_flow(db_session)

    async with _client() as c:
        reg2 = await c.post(
            "/api/mcp-server/register",
            json={"client_name": "other", "redirect_uris": ["http://localhost:3000/callback"]},
        )
        other_client_id = reg2.json()["client_id"]

        mismatched = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "refresh_token",
                "client_id": other_client_id,
                "refresh_token": refresh_raw,
            },
        )
        legitimate = await c.post(
            "/api/mcp-server/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_raw,
            },
        )
    assert mismatched.status_code == 401
    assert mismatched.json()["error"] == "invalid_grant"
    assert legitimate.status_code == 200, legitimate.text
    assert len(legitimate.json()["refresh_token"]) > 20


# ---------------------------------------------------------------------------
# Revoke + sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_tokens_for_user_clears_all(db_session) -> None:
    """revoke_tokens_for_user removes all access + refresh rows for the user."""
    from app.domain.mcp_server.models import McpOAuthClientRow  # noqa: PLC0415

    org = await insert_org(db_session, slug=f"mcp-rev-{uuid4().hex[:8]}")
    user = await create_user(db_session)
    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.org_id,
        role=Role.BUILDER,
        handle="u-rev",
    )
    client_id = uuid4()
    db_session.add(
        McpOAuthClientRow(
            client_id=client_id,
            client_name="t",
            redirect_uris=["http://localhost/cb"],
        )
    )
    await db_session.flush()

    await mint_access_token(client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session)
    await mint_refresh_token(client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session)
    await db_session.flush()

    await revoke_tokens_for_user(user.id, session=db_session)

    acc = (
        (await db_session.execute(select(McpAccessTokenRow).where(McpAccessTokenRow.user_id == user.id)))
        .scalars()
        .all()
    )
    ref = (
        (await db_session.execute(select(McpRefreshTokenRow).where(McpRefreshTokenRow.user_id == user.id)))
        .scalars()
        .all()
    )
    assert acc == []
    assert ref == []


@pytest.mark.asyncio
async def test_identity_delete_user_revokes_mcp_tokens(db_session) -> None:
    """`core/identity.delete_user` runs the user-deletion hook registered by
    `domain/mcp_server` at import time — MCP bearers cannot outlive the user
    row (the token tables carry no FK to `users`, so no DB cascade applies)."""
    from app.core.identity import delete_user as _identity_delete_user  # noqa: PLC0415
    from app.domain.mcp_server.models import McpOAuthClientRow  # noqa: PLC0415

    org = await insert_org(db_session, slug=f"mcp-del-{uuid4().hex[:8]}")
    user = await create_user(db_session)
    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.org_id,
        role=Role.BUILDER,
        handle="u-del",
    )
    client_id = uuid4()
    db_session.add(
        McpOAuthClientRow(
            client_id=client_id,
            client_name="t",
            redirect_uris=["http://localhost/cb"],
        )
    )
    await db_session.flush()

    await mint_access_token(client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session)
    await mint_refresh_token(client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session)
    await db_session.flush()

    await _identity_delete_user(db_session, user_id=user.id)

    acc = (
        (await db_session.execute(select(McpAccessTokenRow).where(McpAccessTokenRow.user_id == user.id)))
        .scalars()
        .all()
    )
    ref = (
        (await db_session.execute(select(McpRefreshTokenRow).where(McpRefreshTokenRow.user_id == user.id)))
        .scalars()
        .all()
    )
    assert acc == []
    assert ref == []


@pytest.mark.asyncio
async def test_sweep_deletes_expired_rows(db_session) -> None:
    """_sweep_expired_tokens removes expired access + refresh rows, leaves fresh ones."""
    from app.domain.mcp_server.models import McpOAuthClientRow  # noqa: PLC0415

    org = await insert_org(db_session, slug=f"mcp-swp-{uuid4().hex[:8]}")
    user = await create_user(db_session)
    await create_membership(
        db_session,
        user_id=user.id,
        org_id=org.org_id,
        role=Role.BUILDER,
        handle="u-swp",
    )
    client_id = uuid4()
    db_session.add(
        McpOAuthClientRow(
            client_id=client_id,
            client_name="t",
            redirect_uris=["http://localhost/cb"],
        )
    )
    await db_session.flush()

    expired_access = await mint_access_token(
        client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session
    )
    expired_refresh = await mint_refresh_token(
        client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session
    )
    fresh_access = await mint_access_token(
        client_id=client_id, user_id=user.id, org_id=org.org_id, session=db_session
    )
    await db_session.flush()

    # Backdate the expired rows.
    for row in (
        (
            await db_session.execute(
                select(McpAccessTokenRow).where(McpAccessTokenRow.token_hash == _hash_token(expired_access))
            )
        ).scalar_one(),
        (
            await db_session.execute(
                select(McpRefreshTokenRow).where(
                    McpRefreshTokenRow.token_hash == _hash_token(expired_refresh)
                )
            )
        ).scalar_one(),
    ):
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    await _sweep_expired_tokens()

    db_session.expire_all()
    acc_gone = (
        await db_session.execute(
            select(McpAccessTokenRow).where(McpAccessTokenRow.token_hash == _hash_token(expired_access))
        )
    ).scalar_one_or_none()
    ref_gone = (
        await db_session.execute(
            select(McpRefreshTokenRow).where(McpRefreshTokenRow.token_hash == _hash_token(expired_refresh))
        )
    ).scalar_one_or_none()
    fresh_still_there = (
        await db_session.execute(
            select(McpAccessTokenRow).where(McpAccessTokenRow.token_hash == _hash_token(fresh_access))
        )
    ).scalar_one_or_none()

    assert acc_gone is None
    assert ref_gone is None
    assert fresh_still_there is not None


@pytest.mark.asyncio
async def test_sweep_task_registered_with_broker() -> None:
    """The sweep task is registered with the taskiq broker under its public name."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    assert get_broker().find_task("mcp_server_token_sweep") is not None
    assert mcp_server_token_sweep is not None
