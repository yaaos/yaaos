"""Generic user-OAuth connection subsystem for `core/oauth`.

Owns:
- Pydantic value objects: `UserOAuthConnection`, `DeviceAuthStart`,
  `UserOAuthCredential`, and the status literal `DeviceAuthStatus`.
- Error types: `ConnectionMissingError`, `ConnectionNeedsReauthError`.
- `UserOAuthApp` frozen dataclass + module-private registry.
- `register_user_oauth_app`, `get_user_oauth_app`, `list_user_oauth_apps`.
- Per-user device-auth connect flow and disconnect:
    `get_user_connection`, `start_device_auth`, `poll_device_auth`,
    `disconnect_user_connection`.

All write services are shape-(a): take `session: AsyncSession`, never commit.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, SecretStr
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.audit_log import audit as _audit_write
from app.core.oauth.models import UserOAuthConnectionRow, UserOAuthDeviceSessionRow
from app.core.oauth.service import (
    OAuthError,
    TokenEndpointSpec,
    Tokens,
    _post_device_authorize,
    _post_token,
)
from app.core.secrets import decrypt, encrypt

log = structlog.get_logger("core.oauth.user_connections")


# ---------------------------------------------------------------------------
# Value objects (no token material — safe to cross module boundaries)
# ---------------------------------------------------------------------------


class UserOAuthConnection(BaseModel):
    """Public view of a user-OAuth connection row. Contains no token material."""

    model_config = {"frozen": True}

    user_id: UUID
    provider_id: str
    status: Literal["connected", "needs_reauth"]
    external_account_id: str | None
    connected_at: datetime
    needs_reauth_reason: str | None


class DeviceAuthStart(BaseModel):
    """Result of `start_device_auth` — the details to display in the connect dialog."""

    model_config = {"frozen": True}

    verification_url: str
    user_code: str
    expires_at: datetime
    poll_interval_seconds: int


class UserOAuthCredential(BaseModel):
    """Token material for a connected OAuth app. Unwrap `.access_token` only
    at the wire boundary (subprocess env, auth.json write)."""

    model_config = {"frozen": True}

    access_token: SecretStr
    id_token: SecretStr | None
    external_account_id: str | None
    expires_at: datetime


# DeviceAuthStatus is a Literal alias, not a class.
DeviceAuthStatus = Literal["pending", "connected", "denied", "expired", "none"]


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class ConnectionMissingError(LookupError):
    """Raised when `ensure_fresh_access_token` finds no connection row."""


class ConnectionNeedsReauthError(Exception):
    """Raised when the connection requires re-authorization."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


# ---------------------------------------------------------------------------
# UserOAuthApp registration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserOAuthApp:
    """Per-provider registration for the generic user-connection subsystem.

    The codex plugin is its first registrant. Any future OAuth app adds its own
    `UserOAuthApp` via `register_user_oauth_app` in its plugin bootstrap.

    `flow` is the extension point for future connect-flow variants. Today only
    `"device_code"` (RFC-8628) is implemented.
    """

    provider_id: str
    display_name: str
    connect_hint: str
    flow: Literal["device_code"]
    device_authorize_url: str
    token_url: str
    client_id: str
    client_secret: SecretStr | None  # None → public client (codex uses this)
    default_scopes: tuple[str, ...]
    token_auth_style: Literal["form", "basic"] = "form"
    scope_separator: str = " "
    expiry_source: Literal["expires_in", "jwt_exp"] = "expires_in"
    capture_id_token: bool = False
    account_id_extractor: Callable[[Tokens], str | None] | None = field(
        default=None, hash=False, compare=False
    )
    refresh_after_seconds: int = 345600  # 4 days
    # DI seams — set only in tests; production leaves as None (uses module defaults).
    device_authorize_fn: Callable[..., Awaitable[dict[str, Any]]] | None = field(
        default=None, hash=False, compare=False
    )
    token_fn: Callable[..., Awaitable[Tokens]] | None = field(default=None, hash=False, compare=False)


# Module-private registry — same shape as core/api_keys validator registry.
_APPS: dict[str, UserOAuthApp] = {}


def register_user_oauth_app(app: UserOAuthApp) -> None:
    """Register a provider. Raises ValueError on duplicate provider_id."""
    if app.provider_id in _APPS:
        raise ValueError(f"UserOAuthApp already registered: {app.provider_id!r}")
    _APPS[app.provider_id] = app


def get_user_oauth_app(provider_id: str) -> UserOAuthApp:
    """Raises LookupError when the provider is not registered."""
    try:
        return _APPS[provider_id]
    except KeyError:
        raise LookupError(f"no UserOAuthApp registered for provider_id={provider_id!r}")


def list_user_oauth_apps() -> list[UserOAuthApp]:
    return list(_APPS.values())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_jwt_exp(token: str) -> datetime | None:
    """Extract `exp` from a JWT payload without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        padding = 4 - len(parts[1]) % 4
        padded = parts[1] + "=" * (padding % 4)
        payload_bytes = base64.urlsafe_b64decode(padded)
        data = json.loads(payload_bytes)
        exp = data.get("exp")
        if exp:
            return datetime.fromtimestamp(float(exp), tz=UTC)
        return None
    except Exception:
        return None


def _compute_expires_at(app: UserOAuthApp, tokens: Tokens) -> datetime:
    """Compute the absolute `access_token_expires_at` from the app's expiry source."""
    if app.expiry_source == "jwt_exp":
        exp = _extract_jwt_exp(tokens.access_token.get_secret_value())
        if exp is not None:
            return exp
        # Fallback: treat expires_in as if jwt_exp were missing.
    return datetime.now(UTC) + timedelta(seconds=tokens.expires_in)


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


class _ConnectedPayload(BaseModel):
    provider_id: str
    external_account_id: str | None


class _DisconnectedPayload(BaseModel):
    provider_id: str
    external_account_id: str | None


async def _emit_connection_audit(
    session: AsyncSession,
    *,
    user_id: UUID,
    kind: str,
    provider_id: str,
    external_account_id: str | None,
) -> None:
    """Write one audit row per org the user belongs to (mirrors _emit_login_audit)."""
    from app.core.tenancy import list_memberships_for_user as _list_memberships  # noqa: PLC0415

    payload: BaseModel
    if kind == "oauth_connection.connected":
        payload = _ConnectedPayload(provider_id=provider_id, external_account_id=external_account_id)
    else:
        payload = _DisconnectedPayload(provider_id=provider_id, external_account_id=external_account_id)
    actor = Actor.user(user_id=user_id)
    memberships = await _list_memberships(session, user_id)
    for m in memberships:
        await _audit_write(
            "user",
            user_id,
            kind,
            payload,
            actor,
            org_id=m.org_id,
            session=session,
        )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


async def get_user_connection(
    user_id: UUID,
    provider_id: str,
    *,
    session: AsyncSession,
) -> UserOAuthConnection | None:
    """Return the connection VO, or None if no row exists."""
    row = (
        await session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == provider_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return UserOAuthConnection(
        user_id=row.user_id,
        provider_id=row.provider_id,
        status=row.status,  # type: ignore[arg-type]
        external_account_id=row.external_account_id,
        connected_at=row.created_at,
        needs_reauth_reason=row.needs_reauth_reason,
    )


async def start_device_auth(
    user_id: UUID,
    provider_id: str,
    *,
    session: AsyncSession,
) -> DeviceAuthStart:
    """Begin a device-auth handshake for `provider_id`.

    Calls the device-authorization endpoint, upserts the pending session row
    (replacing any existing one for this user + provider), and returns the
    details for the connect dialog.

    Raises LookupError when the provider is not registered.
    Raises OAuthError on provider endpoint failure.
    """
    app = get_user_oauth_app(provider_id)

    _device_authorize = app.device_authorize_fn or _post_device_authorize
    body = await _device_authorize(
        device_authorize_url=app.device_authorize_url,
        client_id=app.client_id,
        scopes=app.default_scopes,
        scope_separator=app.scope_separator,
    )

    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_uri = body.get("verification_uri_complete") or body.get("verification_uri")
    expires_in = int(body.get("expires_in") or 900)
    interval = int(body.get("interval") or 5)

    if not device_code:
        raise OAuthError("device-authorize response missing device_code")

    encrypted_device_code = encrypt(device_code).decode()
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

    # Upsert: ON CONFLICT (user_id, provider_id) DO UPDATE replaces an existing
    # pending session so re-starting the flow always uses fresh codes.
    stmt = (
        pg_insert(UserOAuthDeviceSessionRow)
        .values(
            user_id=user_id,
            provider_id=provider_id,
            encrypted_device_code=encrypted_device_code,
            user_code=user_code,
            verification_url=verification_uri,
            poll_interval_seconds=interval,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "provider_id"],
            set_={
                "encrypted_device_code": encrypted_device_code,
                "user_code": user_code,
                "verification_url": verification_uri,
                "poll_interval_seconds": interval,
                "expires_at": expires_at,
                "created_at": datetime.now(UTC),
            },
        )
    )
    await session.execute(stmt)
    await session.flush()

    return DeviceAuthStart(
        verification_url=verification_uri or "",
        user_code=user_code or "",
        expires_at=expires_at,
        poll_interval_seconds=interval,
    )


async def poll_device_auth(
    user_id: UUID,
    provider_id: str,
    *,
    actor: Actor,
    session: AsyncSession,
) -> DeviceAuthStatus:
    """Poll the provider token endpoint for a pending device-auth handshake.

    One token-endpoint call per invocation. Updates the session row interval
    on `slow_down`; stores tokens and deletes the session on grant; deletes
    the session on terminal denial or expiry.

    Returns a `DeviceAuthStatus` literal.

    Raises LookupError when the provider is not registered.
    Raises OAuthError for genuine provider errors (not RFC-8628 flow signals).
    """
    app = get_user_oauth_app(provider_id)

    session_row = (
        await session.execute(
            select(UserOAuthDeviceSessionRow).where(
                UserOAuthDeviceSessionRow.user_id == user_id,
                UserOAuthDeviceSessionRow.provider_id == provider_id,
            )
        )
    ).scalar_one_or_none()
    if session_row is None:
        return "none"

    device_code = decrypt(session_row.encrypted_device_code.encode()).decode()
    spec = TokenEndpointSpec(
        url=app.token_url,
        client_id=app.client_id,
        client_secret=app.client_secret,
        token_auth_style=app.token_auth_style,
    )
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": app.client_id,
    }

    _token = app.token_fn or _post_token
    try:
        tokens = await _token(spec, data)
    except OAuthError as exc:
        code = exc.error_code
        if code == "authorization_pending":
            return "pending"
        if code == "slow_down":
            await session.execute(
                pg_insert(UserOAuthDeviceSessionRow)
                .values(
                    user_id=user_id,
                    provider_id=provider_id,
                    encrypted_device_code=session_row.encrypted_device_code,
                    user_code=session_row.user_code,
                    verification_url=session_row.verification_url,
                    poll_interval_seconds=session_row.poll_interval_seconds + 5,
                    expires_at=session_row.expires_at,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "provider_id"],
                    set_={"poll_interval_seconds": session_row.poll_interval_seconds + 5},
                )
            )
            await session.flush()
            # Expire the cached row so next read reflects the DB value.
            session.expire(session_row)
            return "pending"
        if code in ("access_denied", "expired_token"):
            await session.execute(
                delete(UserOAuthDeviceSessionRow).where(
                    UserOAuthDeviceSessionRow.user_id == user_id,
                    UserOAuthDeviceSessionRow.provider_id == provider_id,
                )
            )
            await session.flush()
            return "denied" if code == "access_denied" else "expired"
        # Genuine error — re-raise so the route maps to 502.
        raise

    # --- Grant received ---
    now = datetime.now(UTC)
    expires_at = _compute_expires_at(app, tokens)
    external_account_id: str | None = None
    if app.account_id_extractor is not None:
        try:
            external_account_id = app.account_id_extractor(tokens)
        except Exception:
            log.warning("oauth.account_id_extractor_failed", provider_id=provider_id)

    encrypted_access_token = encrypt(tokens.access_token.get_secret_value()).decode()
    encrypted_refresh_token = (
        encrypt(tokens.refresh_token.get_secret_value()).decode()
        if tokens.refresh_token is not None
        else None
    )
    encrypted_id_token: str | None = None
    if app.capture_id_token and tokens.id_token is not None:
        encrypted_id_token = encrypt(tokens.id_token.get_secret_value()).decode()

    # Upsert the connection row.
    stmt = (
        pg_insert(UserOAuthConnectionRow)
        .values(
            user_id=user_id,
            provider_id=provider_id,
            status="connected",
            encrypted_access_token=encrypted_access_token,
            encrypted_refresh_token=encrypted_refresh_token,
            encrypted_id_token=encrypted_id_token,
            external_account_id=external_account_id,
            granted_scope=tokens.scope or None,
            access_token_expires_at=expires_at,
            last_refresh_at=now,
            needs_reauth_reason=None,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "provider_id"],
            set_={
                "status": "connected",
                "encrypted_access_token": encrypted_access_token,
                "encrypted_refresh_token": encrypted_refresh_token,
                "encrypted_id_token": encrypted_id_token,
                "external_account_id": external_account_id,
                "granted_scope": tokens.scope or None,
                "access_token_expires_at": expires_at,
                "last_refresh_at": now,
                "needs_reauth_reason": None,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)

    # Delete the consumed pending session.
    await session.execute(
        delete(UserOAuthDeviceSessionRow).where(
            UserOAuthDeviceSessionRow.user_id == user_id,
            UserOAuthDeviceSessionRow.provider_id == provider_id,
        )
    )
    await session.flush()

    # Audit: one row per org membership.
    await _emit_connection_audit(
        session,
        user_id=user_id,
        kind="oauth_connection.connected",
        provider_id=provider_id,
        external_account_id=external_account_id,
    )

    return "connected"


async def disconnect_user_connection(
    user_id: UUID,
    provider_id: str,
    *,
    actor: Actor,
    session: AsyncSession,
) -> bool:
    """Delete the connection row. Audits `oauth_connection.disconnected` iff removed.

    NEVER calls a provider revoke endpoint — disconnect is delete-only.

    Returns True when a row was removed, False when nothing was stored.
    """
    row = (
        await session.execute(
            select(UserOAuthConnectionRow).where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == provider_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    external_account_id = row.external_account_id
    await session.execute(
        delete(UserOAuthConnectionRow).where(
            UserOAuthConnectionRow.user_id == user_id,
            UserOAuthConnectionRow.provider_id == provider_id,
        )
    )
    await session.flush()

    await _emit_connection_audit(
        session,
        user_id=user_id,
        kind="oauth_connection.disconnected",
        provider_id=provider_id,
        external_account_id=external_account_id,
    )
    return True
