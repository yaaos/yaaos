"""Per-(org, provider) hosted-MCP credential lifecycle.

This module owns `mcp_credentials`. Provider plugins (`plugins/linear`,
`plugins/notion`) register themselves with `register_provider(...)` at
boot; this service consumes the registry and stays free of plugin imports.

Public ops:

- `get(session, org_id, provider)` — return the row or None.
- `connect_callback(session, *, provider, code, org_id, redirect_uri, actor, upstream_identity=None)` —
  exchange the code via `core/oauth`, persist encrypted tokens, audit
  `mcp.<provider>.connected`.
- `clear(session, *, org_id, provider, actor)` — delete the row, audit
  `mcp.<provider>.disconnected`.
- `validate(session, *, org_id, provider, actor)` — call the plugin's `validate(...)`,
  flip `last_refresh_status`, audit `mcp.<provider>.validated`.
- `update_allowlist(session, *, org_id, provider, allowed_tools, actor)` — replace the
  per-tool allowlist, audit `mcp.<provider>.allowlist_updated`.
- `list_broken_credentials_for_org(session, org_id)` — return enabled credentials
  where `last_refresh_status == "failed"` as `McpCredential` value objects.
- `create_credential(session, *, org_id, provider, ...)` — insert a new credential row
  (used by seed/test helpers that need a known state without going through OAuth).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from pydantic import BaseModel, SecretStr
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


class McpCredential(BaseModel):
    """Metadata value object — a single (org, provider) MCP credential.

    Carries no secret material. Call sites that must decrypt the upstream
    token fetch `McpCredentialSecret` via `get_secret` and decrypt at the
    point of use.
    """

    org_id: UUID
    provider: str
    enabled: bool
    last_refresh_status: str | None
    last_refresh_failed_at: datetime | None
    upstream_identity: str | None
    allowed_tools: list[str]
    expires_at: datetime

    @classmethod
    def from_row(cls, row: McpCredentialRow) -> McpCredential:
        return cls(
            org_id=row.org_id,
            provider=row.provider,
            enabled=row.enabled,
            last_refresh_status=row.last_refresh_status,
            last_refresh_failed_at=row.last_refresh_failed_at,
            upstream_identity=row.upstream_identity,
            allowed_tools=list(row.allowed_tools or []),
            expires_at=row.expires_at,
        )


class McpCredentialSecret(BaseModel):
    """Secret value object — the encrypted upstream access token only.

    Returned solely to call sites that decrypt at the point of use, keeping
    secret material out of the `McpCredential` metadata VO.
    """

    encrypted_access_token: str


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


async def _get_row(session: AsyncSession, org_id: UUID, provider: str) -> McpCredentialRow | None:
    """Return the raw `McpCredentialRow` for intra-module use (mutations)."""
    return (
        await session.execute(
            select(McpCredentialRow).where(
                McpCredentialRow.org_id == org_id,
                McpCredentialRow.provider == provider,
            )
        )
    ).scalar_one_or_none()


async def get(session: AsyncSession, org_id: UUID, provider: str) -> McpCredential | None:
    row = await _get_row(session, org_id, provider)
    return McpCredential.from_row(row) if row is not None else None


async def get_secret(session: AsyncSession, org_id: UUID, provider: str) -> McpCredentialSecret | None:
    """Return the encrypted access token for (org, provider), or None.

    Separate from `get` so secret material never rides on the metadata VO.
    Callers decrypt via `core/secrets.decrypt` at the point of use.
    """
    row = await _get_row(session, org_id, provider)
    return McpCredentialSecret(encrypted_access_token=row.encrypted_access_token) if row is not None else None


async def connect_callback(
    session: AsyncSession,
    *,
    provider: str,
    code: str,
    org_id: UUID,
    redirect_uri: str,
    actor: Actor,
    upstream_identity: str | None = None,
) -> McpCredential:
    """Exchange the OAuth code, persist encrypted tokens, emit audit."""
    prov = get_provider(provider)
    if prov is None:
        raise ProviderNotRegisteredError(provider)

    tokens = await exchange_code(prov.config, code=code, redirect_uri=redirect_uri)
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens.expires_in)

    row = await _get_row(session, org_id, provider)
    encrypted_access = encrypt(tokens.access_token.get_secret_value()).decode()
    encrypted_refresh = (
        encrypt(tokens.refresh_token.get_secret_value()).decode() if tokens.refresh_token else None
    )
    if row is None:
        row = McpCredentialRow(
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
        session.add(row)
    else:
        row.encrypted_access_token = encrypted_access
        row.encrypted_refresh_token = encrypted_refresh
        row.expires_at = expires_at
        row.scopes = tokens.scope.split() if tokens.scope else []
        row.enabled = True
        if upstream_identity is not None:
            row.upstream_identity = upstream_identity
        row.last_refresh_status = "ok"
        row.last_refresh_failed_at = None
        row.last_validated_at = datetime.now(UTC)
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
    return McpCredential.from_row(row)


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
    row = await _get_row(session, org_id, provider)
    if row is None:
        raise IntegrationNotConnectedError(provider)
    try:
        access = SecretStr(decrypt(row.encrypted_access_token.encode()).decode())
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
) -> McpCredential:
    row = await _get_row(session, org_id, provider)
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
    return McpCredential.from_row(row)


async def list_broken_credentials_for_org(
    session: AsyncSession,
    org_id: UUID,
) -> list[McpCredential]:
    """Return enabled credentials with `last_refresh_status == "failed"` for *org_id*."""
    rows = (
        (
            await session.execute(
                select(McpCredentialRow).where(
                    McpCredentialRow.org_id == org_id,
                    McpCredentialRow.enabled.is_(True),
                    McpCredentialRow.last_refresh_status == "failed",
                )
            )
        )
        .scalars()
        .all()
    )
    return [McpCredential.from_row(r) for r in rows]


async def create_credential(
    session: AsyncSession,
    *,
    org_id: UUID,
    provider: str,
    encrypted_access_token: str,
    encrypted_refresh_token: str | None = None,
    expires_at: datetime,
    scopes: list[str],
    allowed_tools: list[str] | None = None,
    enabled: bool = True,
    upstream_identity: str | None = None,
    last_refresh_status: str | None = None,
    last_refresh_failed_at: datetime | None = None,
) -> McpCredential:
    """Insert a new `mcp_credentials` row and flush."""
    row = McpCredentialRow(
        org_id=org_id,
        provider=provider,
        encrypted_access_token=encrypted_access_token,
        encrypted_refresh_token=encrypted_refresh_token,
        expires_at=expires_at,
        scopes=scopes,
        allowed_tools=allowed_tools or [],
        enabled=enabled,
        upstream_identity=upstream_identity,
        last_refresh_status=last_refresh_status,
        last_refresh_failed_at=last_refresh_failed_at,
    )
    session.add(row)
    await session.flush()
    return McpCredential.from_row(row)


async def mark_last_used(
    session: AsyncSession,
    *,
    org_id: UUID,
    provider: str,
) -> None:
    """Stamp `last_used_at` on the credential row. No-op if the row is gone."""
    from datetime import UTC as _UTC  # noqa: PLC0415
    from datetime import datetime as _datetime  # noqa: PLC0415

    row = await _get_row(session, org_id, provider)
    if row is not None:
        row.last_used_at = _datetime.now(_UTC)
        await session.flush()
