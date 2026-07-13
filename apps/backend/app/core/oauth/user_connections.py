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
- Token refresh lifecycle:
    `ensure_fresh_access_token` (shape-b; opens own session, commits rotation before returning),
    `refresh_due_connections` (TaskRef for `@scheduled` hourly sweeper),
    `_do_refresh_due_connections` (private body — call directly in tests).

All shape-(a) services take `session: AsyncSession`, never commit.
`ensure_fresh_access_token` is shape-b (rotation-commit carve-out — see patterns.md).
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
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor
from app.core.audit_log import audit as _audit_write
from app.core.auth import org_id_var
from app.core.database import session as db_session
from app.core.notifications import create as _notification_create
from app.core.oauth.models import UserOAuthConnectionRow, UserOAuthDeviceSessionRow
from app.core.oauth.service import (
    OAuthError,
    TokenEndpointSpec,
    Tokens,
    _post_device_authorize,
    _post_token,
)
from app.core.secrets import decrypt, encrypt
from app.core.tasks import scheduled
from app.core.tenancy import list_memberships_for_user

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
    # Relevance predicate — "should this provider appear on this user's Connections
    # page?" None means always visible. See `list_visible_user_oauth_apps`.
    relevance_fn: Callable[[UUID, AsyncSession], Awaitable[bool]] | None = field(
        default=None, hash=False, compare=False
    )


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


async def list_visible_user_oauth_apps(user_id: UUID, *, session: AsyncSession) -> list[UserOAuthApp]:
    """Return registered apps relevant to this user's Connections page.

    An app is visible when any of the following holds:
    - it has no `relevance_fn` (always visible), or
    - the user already has a connection row for it (connected or needs_reauth —
      keeps the card reachable for disconnect after an org switches away from it), or
    - `relevance_fn(user_id, session)` returns True.
    """
    visible: list[UserOAuthApp] = []
    for app in _APPS.values():
        if app.relevance_fn is None:
            visible.append(app)
            continue
        if await get_user_connection(user_id, app.provider_id, session=session) is not None:
            visible.append(app)
            continue
        if await app.relevance_fn(user_id, session):
            visible.append(app)
    return visible


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
# Refresh lifecycle — constants and helpers
# ---------------------------------------------------------------------------

# OAuthError.error_code values that are terminal (never retry; user must reconnect).
# Everything else (5xx, transport, unknown) is transient — leave the row connected
# and let the next refresh pass retry.
_TERMINAL_ERROR_CODES: frozenset[str] = frozenset({"invalid_grant", "refresh_token_reused", "access_denied"})


def _row_to_credential(app: UserOAuthApp, row: UserOAuthConnectionRow) -> UserOAuthCredential:
    """Decrypt + wrap a connection row into a `UserOAuthCredential` VO."""
    access_token = decrypt(row.encrypted_access_token.encode()).decode()
    id_token: str | None = None
    if row.encrypted_id_token is not None:
        id_token = decrypt(row.encrypted_id_token.encode()).decode()
    return UserOAuthCredential(
        access_token=SecretStr(access_token),
        id_token=SecretStr(id_token) if id_token is not None else None,
        external_account_id=row.external_account_id,
        expires_at=row.access_token_expires_at,
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

    # Server-side cooldown: if a poll already fired within the last
    # `poll_interval_seconds`, return "pending" immediately without hitting the
    # provider. This guards against clients that ignore the RFC-8628 interval.
    now = datetime.now(UTC)
    if session_row.last_polled_at is not None:
        elapsed = (now - session_row.last_polled_at).total_seconds()
        if elapsed < session_row.poll_interval_seconds:
            return "pending"

    # Stamp last_polled_at before the upstream call so concurrent requests also
    # see the cooldown immediately (best-effort; not a strict lock).
    # Direct ORM assignment keeps the in-memory object consistent for same-session
    # callers (e.g. tests that call poll_device_auth twice in one session).
    session_row.last_polled_at = now
    await session.flush()

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


async def _notify_needs_reauth(s: AsyncSession, *, user_id: UUID, provider_id: str, reason: str) -> None:
    """Create a user notification for a connected → needs_reauth flip.

    Best-effort — never raises: the flip's commit must not be lost to a
    notification failure. Org attribution: the current org context when set
    (dispatch/claim paths), else the user's first membership org (scheduler
    path); skipped with a WARN when the user has no membership.
    """
    try:
        app = get_user_oauth_app(provider_id)
        org_id = org_id_var.get()
        if org_id is None:
            memberships = await list_memberships_for_user(s, user_id)
            org_id = memberships[0].org_id if memberships else None
        if org_id is None:
            log.warning(
                "oauth.needs_reauth_notification_skipped",
                user_id=str(user_id),
                provider_id=provider_id,
                skip_reason="user has no org membership",
            )
            return
        await _notification_create(
            user_id=user_id,
            org_id=org_id,
            type="oauth_connection_needs_reauth",
            title=f"{app.display_name} connection needs re-authorization",
            body=(
                f"{reason}. Runs that use this connection will fail until you "
                "reconnect under User settings → Details → Connections."
            ),
            session=s,
        )
    except Exception as exc:
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(StatusCode.ERROR, "needs_reauth_notification_failed")
        log.exception(
            "oauth.needs_reauth_notification_failed",
            user_id=str(user_id),
            provider_id=provider_id,
        )


async def ensure_fresh_access_token(
    user_id: UUID,
    provider_id: str,
    *,
    min_remaining_seconds: int = 300,
) -> UserOAuthCredential:
    """Return a valid credential for the connection, refreshing the token if stale.

    Shape-b (opens own short DB session, commits before returning).  The caller
    MUST NOT pass a session — rotation commits before control returns.

    Rationale for shape-b: the provider invalidates the old refresh token
    server-side on the first successful call.  If rotation rode a caller
    transaction that later rolled back, the persisted token would be dead.

    Algorithm:
    1. Fast path (non-locking): if token has ≥ `min_remaining_seconds` of life, return.
    2. Slow path (FOR UPDATE): re-read row under lock, re-check freshness (a
       concurrent caller may have refreshed while we waited for the lock), then
       call the token endpoint, persist the new tokens, and commit atomically.

    Raises:
        LookupError: provider not registered.
        ConnectionMissingError: no row exists for (user_id, provider_id).
        ConnectionNeedsReauthError: row is `needs_reauth`, or a terminal OAuth error
            (`invalid_grant`, `refresh_token_reused`, `access_denied`) flips it.
        OAuthError: transient provider error; row stays `connected`, safe to retry.
    """
    app = get_user_oauth_app(provider_id)
    threshold = timedelta(seconds=min_remaining_seconds)
    now = datetime.now(UTC)

    # --- Fast path: non-locking read ---
    async with db_session() as s:
        row = (
            await s.execute(
                select(UserOAuthConnectionRow).where(
                    UserOAuthConnectionRow.user_id == user_id,
                    UserOAuthConnectionRow.provider_id == provider_id,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            raise ConnectionMissingError(
                f"No OAuth connection for user_id={user_id}, provider_id={provider_id!r}"
            )

        if row.status == "needs_reauth":
            raise ConnectionNeedsReauthError(
                f"OAuth connection requires re-authorization: {row.needs_reauth_reason}"
            )

        if row.access_token_expires_at > now + threshold:
            # Token has enough remaining life; no lock or provider call needed.
            return _row_to_credential(app, row)

    # --- Slow path: lock, re-check, refresh ---
    async with db_session() as s:
        row = (
            await s.execute(
                select(UserOAuthConnectionRow)
                .where(
                    UserOAuthConnectionRow.user_id == user_id,
                    UserOAuthConnectionRow.provider_id == provider_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if row is None:
            raise ConnectionMissingError(
                f"No OAuth connection for user_id={user_id}, provider_id={provider_id!r}"
            )

        if row.status == "needs_reauth":
            raise ConnectionNeedsReauthError(
                f"OAuth connection requires re-authorization: {row.needs_reauth_reason}"
            )

        # Re-check after acquiring lock: a concurrent caller may have already refreshed.
        if row.access_token_expires_at > datetime.now(UTC) + threshold:
            return _row_to_credential(app, row)

        if row.encrypted_refresh_token is None:
            # No refresh token — the provider issued access-only; user must reconnect.
            raise ConnectionNeedsReauthError(
                f"No refresh token available for provider_id={provider_id!r}; reconnect required"
            )

        # --- Token endpoint call ---
        refresh_token_plaintext = decrypt(row.encrypted_refresh_token.encode()).decode()
        spec = TokenEndpointSpec(
            url=app.token_url,
            client_id=app.client_id,
            client_secret=app.client_secret,
            token_auth_style=app.token_auth_style,
        )
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_plaintext,
            "client_id": app.client_id,
        }

        _token = app.token_fn or _post_token
        try:
            tokens = await _token(spec, data)
        except OAuthError as exc:
            if exc.error_code in _TERMINAL_ERROR_CODES:
                # Flip row to needs_reauth and COMMIT before raising.
                # The provider has rejected the refresh token; persisting the
                # terminal state before the exception unwinds ensures the
                # row reflects reality even if the caller's outer work fails.
                reason = f"token refresh rejected ({exc.error_code})"
                await s.execute(
                    update(UserOAuthConnectionRow)
                    .where(
                        UserOAuthConnectionRow.user_id == user_id,
                        UserOAuthConnectionRow.provider_id == provider_id,
                    )
                    .values(
                        status="needs_reauth",
                        needs_reauth_reason=reason,
                        updated_at=datetime.now(UTC),
                    )
                )
                await _notify_needs_reauth(s, user_id=user_id, provider_id=provider_id, reason=reason)
                await s.commit()
                raise ConnectionNeedsReauthError(
                    f"OAuth connection requires re-authorization ({exc.error_code})"
                ) from exc
            # Transient error — leave the row connected; caller/scheduler retries.
            raise

        # --- Persist rotated tokens ---
        refresh_now = datetime.now(UTC)
        expires_at = _compute_expires_at(app, tokens)
        encrypted_access_token = encrypt(tokens.access_token.get_secret_value()).decode()
        # Keep old refresh token when the provider doesn't rotate it.
        encrypted_refresh_token = (
            encrypt(tokens.refresh_token.get_secret_value()).decode()
            if tokens.refresh_token is not None
            else row.encrypted_refresh_token
        )

        await s.execute(
            update(UserOAuthConnectionRow)
            .where(
                UserOAuthConnectionRow.user_id == user_id,
                UserOAuthConnectionRow.provider_id == provider_id,
            )
            .values(
                encrypted_access_token=encrypted_access_token,
                encrypted_refresh_token=encrypted_refresh_token,
                access_token_expires_at=expires_at,
                last_refresh_at=refresh_now,
                updated_at=refresh_now,
            )
        )
        await s.commit()

        id_token: str | None = None
        if row.encrypted_id_token is not None:
            id_token = decrypt(row.encrypted_id_token.encode()).decode()

        return UserOAuthCredential(
            access_token=tokens.access_token,
            id_token=SecretStr(id_token) if id_token is not None else None,
            external_account_id=row.external_account_id,
            expires_at=expires_at,
        )


async def _do_refresh_due_connections() -> int:
    """One pass of the proactive token-refresh sweeper.

    For each registered provider, selects `connected` rows whose
    `last_refresh_at` is older than the app's `refresh_after_seconds` threshold.
    Each due row is refreshed in its own committed transaction (per-row isolation:
    one failure does not prevent siblings from being updated).

    Also purges expired `user_oauth_device_sessions` rows.

    Returns the number of rows successfully refreshed.
    """
    refreshed = 0
    now = datetime.now(UTC)

    for provider_id, app in _APPS.items():
        cutoff = now - timedelta(seconds=app.refresh_after_seconds)

        async with db_session() as s:
            due_rows = list(
                (
                    await s.execute(
                        select(UserOAuthConnectionRow).where(
                            UserOAuthConnectionRow.provider_id == provider_id,
                            UserOAuthConnectionRow.status == "connected",
                            UserOAuthConnectionRow.last_refresh_at < cutoff,
                        )
                    )
                ).scalars()
            )

        for row in due_rows:
            # Each row uses its own locking transaction for per-row isolation.
            async with db_session() as s:
                locked_row = (
                    await s.execute(
                        select(UserOAuthConnectionRow)
                        .where(
                            UserOAuthConnectionRow.user_id == row.user_id,
                            UserOAuthConnectionRow.provider_id == provider_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                if locked_row is None or locked_row.status != "connected":
                    continue

                # Re-check after lock: was this row refreshed by a concurrent caller?
                if locked_row.last_refresh_at >= cutoff:
                    continue  # already up-to-date; skip to avoid double-refresh

                if locked_row.encrypted_refresh_token is None:
                    log.info(
                        "oauth.token_refresh.skipped_no_refresh_token",
                        user_id=str(row.user_id),
                        provider_id=provider_id,
                    )
                    continue

                refresh_token_plaintext = decrypt(locked_row.encrypted_refresh_token.encode()).decode()
                spec = TokenEndpointSpec(
                    url=app.token_url,
                    client_id=app.client_id,
                    client_secret=app.client_secret,
                    token_auth_style=app.token_auth_style,
                )
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token_plaintext,
                    "client_id": app.client_id,
                }

                _token = app.token_fn or _post_token
                try:
                    tokens = await _token(spec, data)
                except OAuthError as exc:
                    if exc.error_code in _TERMINAL_ERROR_CODES:
                        reason = f"token refresh rejected ({exc.error_code})"
                        await s.execute(
                            update(UserOAuthConnectionRow)
                            .where(
                                UserOAuthConnectionRow.user_id == row.user_id,
                                UserOAuthConnectionRow.provider_id == provider_id,
                            )
                            .values(
                                status="needs_reauth",
                                needs_reauth_reason=reason,
                                updated_at=datetime.now(UTC),
                            )
                        )
                        await _notify_needs_reauth(
                            s, user_id=row.user_id, provider_id=provider_id, reason=reason
                        )
                        await s.commit()
                        log.info(
                            "oauth.token_refresh.needs_reauth",
                            user_id=str(row.user_id),
                            provider_id=provider_id,
                            error_code=exc.error_code,
                        )
                    else:
                        log.warning(
                            "oauth.token_refresh.transient_error",
                            user_id=str(row.user_id),
                            provider_id=provider_id,
                            error=str(exc),
                        )
                    continue
                except Exception as exc:
                    log.error(
                        "oauth.token_refresh.unexpected_error",
                        user_id=str(row.user_id),
                        provider_id=provider_id,
                        error=str(exc),
                    )
                    continue

                refresh_now = datetime.now(UTC)
                expires_at = _compute_expires_at(app, tokens)
                encrypted_access_token = encrypt(tokens.access_token.get_secret_value()).decode()
                encrypted_refresh_token = (
                    encrypt(tokens.refresh_token.get_secret_value()).decode()
                    if tokens.refresh_token is not None
                    else locked_row.encrypted_refresh_token
                )

                await s.execute(
                    update(UserOAuthConnectionRow)
                    .where(
                        UserOAuthConnectionRow.user_id == row.user_id,
                        UserOAuthConnectionRow.provider_id == provider_id,
                    )
                    .values(
                        encrypted_access_token=encrypted_access_token,
                        encrypted_refresh_token=encrypted_refresh_token,
                        access_token_expires_at=expires_at,
                        last_refresh_at=refresh_now,
                        updated_at=refresh_now,
                    )
                )
                await s.commit()
                refreshed += 1

    # Purge expired device sessions (cross-provider; one transaction).
    async with db_session() as s:
        await s.execute(
            delete(UserOAuthDeviceSessionRow).where(UserOAuthDeviceSessionRow.expires_at < datetime.now(UTC))
        )
        await s.commit()

    return refreshed


# Hourly proactive token rotation — cluster-safe via `core/tasks` per-tick claim.
# Exactly one worker pod enqueues per slot. Body is idempotent (per-row FOR UPDATE
# re-check; failed rows are not retried until the next hourly slot).
refresh_due_connections = scheduled(
    name="user_oauth_token_refresh",
    cron="0 * * * *",
    queue="default",
    max_retries=1,
)(_do_refresh_due_connections)
