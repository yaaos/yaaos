"""HTTP routes for `/api/user/oauth/*` — `RouteSecurity.USER_SCOPED`.

| Method | Path                                              | Description              |
|--------|---------------------------------------------------|--------------------------|
| GET    | `/api/user/oauth/connections`                     | List all OAuth apps + status |
| POST   | `/api/user/oauth/{provider_id}/device-auth/start` | Begin device-auth flow |
| POST   | `/api/user/oauth/{provider_id}/device-auth/poll`  | Poll the handshake     |
| DELETE | `/api/user/oauth/{provider_id}/connection`        | Disconnect              |

All routes are USER_SCOPED (session required, no org). Classification derives
from the existing `/api/user/` prefix in `core/auth/types.py` — no change needed.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.audit_log import Actor
from app.core.auth import user_id_var
from app.core.database import session as db_session
from app.core.identity import require_session
from app.core.oauth.models import UserOAuthDeviceSessionRow
from app.core.oauth.service import OAuthError
from app.core.oauth.user_connections import (
    disconnect_user_connection,
    get_user_connection,
    get_user_oauth_app,
    list_visible_user_oauth_apps,
    poll_device_auth,
    start_device_auth,
)
from app.core.webserver import RouteSpec, register_routes

log = structlog.get_logger("core.oauth.user_web")

router = APIRouter()


def _err(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code})


def _require_user_id() -> UUID:
    uid = user_id_var.get()
    if uid is None:
        raise _err(401, "unauthenticated")
    return uid


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class _ConnectionView(BaseModel):
    provider_id: str
    display_name: str
    connect_hint: str
    status: str  # "not_connected" | "connected" | "needs_reauth"
    external_account_id: str | None
    connected_at: datetime | None
    needs_reauth_reason: str | None


class _ConnectionsResponse(BaseModel):
    connections: list[_ConnectionView]


class _DeviceAuthStartResponse(BaseModel):
    verification_url: str
    user_code: str
    expires_at: datetime
    poll_interval_seconds: int


class _DeviceAuthPollResponse(BaseModel):
    status: str
    poll_interval_seconds: int | None


class _DisconnectResponse(BaseModel):
    removed: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/connections", dependencies=[Depends(require_session)])
async def list_connections() -> _ConnectionsResponse:
    """List OAuth apps relevant to the caller with their connection status."""
    user_id = _require_user_id()
    views: list[_ConnectionView] = []
    async with db_session() as s:
        apps = await list_visible_user_oauth_apps(user_id, session=s)
        for app in apps:
            conn = await get_user_connection(user_id, app.provider_id, session=s)
            if conn is None:
                views.append(
                    _ConnectionView(
                        provider_id=app.provider_id,
                        display_name=app.display_name,
                        connect_hint=app.connect_hint,
                        status="not_connected",
                        external_account_id=None,
                        connected_at=None,
                        needs_reauth_reason=None,
                    )
                )
            else:
                views.append(
                    _ConnectionView(
                        provider_id=app.provider_id,
                        display_name=app.display_name,
                        connect_hint=app.connect_hint,
                        status=conn.status,
                        external_account_id=conn.external_account_id,
                        connected_at=conn.connected_at,
                        needs_reauth_reason=conn.needs_reauth_reason,
                    )
                )
    return _ConnectionsResponse(connections=views)


@router.post(
    "/{provider_id}/device-auth/start",
    dependencies=[Depends(require_session)],
)
async def start_device_auth_route(provider_id: str) -> _DeviceAuthStartResponse:
    """Begin a device-auth handshake for `provider_id`."""
    user_id = _require_user_id()
    try:
        get_user_oauth_app(provider_id)
    except LookupError:
        raise _err(404, "unknown_provider")
    try:
        async with db_session() as s:
            result = await start_device_auth(user_id, provider_id, session=s)
            await s.commit()
    except OAuthError as exc:
        log.warning("oauth.device_auth.start_failed", provider_id=provider_id, error=str(exc))
        raise _err(502, "provider_error")
    return _DeviceAuthStartResponse(
        verification_url=result.verification_url,
        user_code=result.user_code,
        expires_at=result.expires_at,
        poll_interval_seconds=result.poll_interval_seconds,
    )


@router.post(
    "/{provider_id}/device-auth/poll",
    dependencies=[Depends(require_session)],
)
async def poll_device_auth_route(provider_id: str) -> _DeviceAuthPollResponse:
    """Poll the device-auth handshake. POST — a successful poll stores tokens and
    writes audit rows (state changes must not be GET requests)."""
    user_id = _require_user_id()
    try:
        get_user_oauth_app(provider_id)
    except LookupError:
        raise _err(404, "unknown_provider")
    actor = Actor.user(user_id=user_id)
    try:
        async with db_session() as s:
            status = await poll_device_auth(user_id, provider_id, actor=actor, session=s)
            # Read the current interval BEFORE commit so it's part of the same tx.
            session_row = (
                await s.execute(
                    select(UserOAuthDeviceSessionRow).where(
                        UserOAuthDeviceSessionRow.user_id == user_id,
                        UserOAuthDeviceSessionRow.provider_id == provider_id,
                    )
                )
            ).scalar_one_or_none()
            current_interval: int | None = (
                session_row.poll_interval_seconds if session_row is not None else None
            )
            await s.commit()
    except OAuthError as exc:
        log.warning("oauth.device_auth.poll_failed", provider_id=provider_id, error=str(exc))
        raise _err(502, "provider_error")
    return _DeviceAuthPollResponse(status=status, poll_interval_seconds=current_interval)


@router.delete(
    "/{provider_id}/connection",
    dependencies=[Depends(require_session)],
)
async def disconnect_connection_route(provider_id: str) -> _DisconnectResponse:
    """Remove the stored connection. Delete-only — never calls a revoke endpoint."""
    user_id = _require_user_id()
    try:
        get_user_oauth_app(provider_id)
    except LookupError:
        raise _err(404, "unknown_provider")
    actor = Actor.user(user_id=user_id)
    async with db_session() as s:
        removed = await disconnect_user_connection(user_id, provider_id, actor=actor, session=s)
        await s.commit()
    return _DisconnectResponse(removed=removed)


register_routes(RouteSpec(module_name="oauth", router=router, url_prefix="/api/user/oauth"))
