"""Per-(org, provider) hosted-MCP credential lifecycle.

This module owns `mcp_credentials`. Provider plugins (`plugins/linear`,
`plugins/notion`) register themselves with `register_provider(...)` at
boot; this service consumes the registry and stays free of plugin imports.

Service surface (Phase 1):

- `get(org_id, provider)` — return the row or None.
- `connect_callback(provider, code, state, org_id, redirect_uri, actor)` —
  exchange the code via `core/oauth`, persist encrypted tokens, audit
  `mcp.<provider>.connected`.
- `clear(org_id, provider, actor)` — delete the row, audit
  `mcp.<provider>.disconnected`.
- `validate(org_id, provider, actor)` — call the plugin's `validate(...)`,
  flip `last_refresh_status`, audit `mcp.<provider>.validated`.
- `update_allowlist(org_id, provider, allowed_tools, actor)` — replace the
  per-tool allowlist, audit `mcp.<provider>.allowlist_updated`.

Advisory-lock-guarded `refresh` ships in a later sub-phase along with the
broken-creds surfacing path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit
from app.core.oauth import exchange_code
from app.core.secrets import SecretsDecryptError, decrypt, encrypt
from app.domain.integrations.models import McpCredentialRow
from app.domain.integrations.types import (
    BrokenCredentialsError,
    IntegrationNotConnectedError,
    ProviderNotRegisteredError,
    get_provider,
)

log = structlog.get_logger("domain.integrations")


class _ConnectedPayload(BaseModel):
    provider: str
    upstream_identity: str | None = None


class _ProviderPayload(BaseModel):
    provider: str


class _ValidatePayload(BaseModel):
    provider: str
    success: bool


class _AllowlistPayload(BaseModel):
    provider: str
    allowed_tools: list[str]


async def get(session: AsyncSession, org_id: UUID, provider: str) -> McpCredentialRow | None:
    return (
        await session.execute(
            select(McpCredentialRow).where(
                McpCredentialRow.org_id == org_id,
                McpCredentialRow.provider == provider,
            )
        )
    ).scalar_one_or_none()


async def connect_callback(
    session: AsyncSession,
    *,
    provider: str,
    code: str,
    org_id: UUID,
    redirect_uri: str,
    actor: Actor,
    upstream_identity: str | None = None,
) -> McpCredentialRow:
    """Exchange the OAuth code, persist encrypted tokens, emit audit."""
    prov = get_provider(provider)
    if prov is None:
        raise ProviderNotRegisteredError(provider)

    tokens = await exchange_code(prov.config, code=code, redirect_uri=redirect_uri)
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)

    existing = await get(session, org_id, provider)
    encrypted_access = encrypt(tokens.access_token).decode()
    encrypted_refresh = encrypt(tokens.refresh_token).decode() if tokens.refresh_token else None
    if existing is None:
        existing = McpCredentialRow(
            org_id=org_id,
            provider=provider,
            encrypted_access_token=encrypted_access,
            encrypted_refresh_token=encrypted_refresh,
            expires_at=expires_at,
            scopes=tokens.scope.split() if tokens.scope else [],
            allowed_tools=[],
            enabled=True,
            upstream_identity=upstream_identity,
            last_refresh_status="ok",
            last_validated_at=datetime.now(UTC),
        )
        session.add(existing)
    else:
        existing.encrypted_access_token = encrypted_access
        existing.encrypted_refresh_token = encrypted_refresh
        existing.expires_at = expires_at
        existing.scopes = tokens.scope.split() if tokens.scope else []
        existing.enabled = True
        if upstream_identity is not None:
            existing.upstream_identity = upstream_identity
        existing.last_refresh_status = "ok"
        existing.last_refresh_failed_at = None
        existing.last_validated_at = datetime.now(UTC)
    await session.flush()
    await audit(
        "org",
        org_id,
        f"mcp.{provider}.connected",
        _ConnectedPayload(provider=provider, upstream_identity=upstream_identity),
        actor,
        org_id=org_id,
        session=session,
    )
    return existing


async def clear(
    session: AsyncSession,
    *,
    org_id: UUID,
    provider: str,
    actor: Actor,
) -> bool:
    """Delete the row. Returns True if a row was removed."""
    result = await session.execute(
        delete(McpCredentialRow).where(
            McpCredentialRow.org_id == org_id,
            McpCredentialRow.provider == provider,
        )
    )
    removed = bool(result.rowcount)
    if removed:
        await audit(
            "org",
            org_id,
            f"mcp.{provider}.disconnected",
            _ProviderPayload(provider=provider),
            actor,
            org_id=org_id,
            session=session,
        )
    return removed


async def validate(
    session: AsyncSession,
    *,
    org_id: UUID,
    provider: str,
    actor: Actor,
) -> bool:
    """Hit the upstream with the stored access token. On success, refresh
    `last_validated_at` + ensure `last_refresh_status = "ok"`. On failure,
    flip status + stamp `last_refresh_failed_at`."""
    prov = get_provider(provider)
    if prov is None:
        raise ProviderNotRegisteredError(provider)
    row = await get(session, org_id, provider)
    if row is None:
        raise IntegrationNotConnectedError(provider)
    try:
        access = decrypt(row.encrypted_access_token.encode()).decode()
    except SecretsDecryptError as exc:
        raise BrokenCredentialsError("could not decrypt access token") from exc

    ok = await prov.validate(access)
    now = datetime.now(UTC)
    if ok:
        row.last_validated_at = now
        row.last_refresh_status = "ok"
        row.last_refresh_failed_at = None
    else:
        row.last_refresh_status = "failed"
        row.last_refresh_failed_at = now
    await session.flush()
    await audit(
        "org",
        org_id,
        f"mcp.{provider}.validated",
        _ValidatePayload(provider=provider, success=ok),
        actor,
        org_id=org_id,
        session=session,
    )
    return ok


async def update_allowlist(
    session: AsyncSession,
    *,
    org_id: UUID,
    provider: str,
    allowed_tools: list[str],
    actor: Actor,
) -> McpCredentialRow:
    row = await get(session, org_id, provider)
    if row is None:
        raise IntegrationNotConnectedError(provider)
    row.allowed_tools = list(allowed_tools)
    await session.flush()
    await audit(
        "org",
        org_id,
        f"mcp.{provider}.allowlist_updated",
        _AllowlistPayload(provider=provider, allowed_tools=list(allowed_tools)),
        actor,
        org_id=org_id,
        session=session,
    )
    return row
