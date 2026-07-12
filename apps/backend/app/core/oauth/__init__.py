"""core/oauth — generic OAuth 2.0 authorization-code + refresh primitives,
and the user-connection subsystem (device-auth connect/disconnect, token storage)."""

import app.core.oauth.user_web  # noqa: F401 — registers /api/user/oauth/* routes
from app.core.oauth.service import (
    OAuthError,
    ProviderConfig,
    Tokens,
    build_authorize_url,
    exchange_code,
    refresh_access_token,
)
from app.core.oauth.user_connections import (
    ConnectionMissingError,
    ConnectionNeedsReauthError,
    DeviceAuthStart,
    UserOAuthApp,
    UserOAuthConnection,
    UserOAuthCredential,
    disconnect_user_connection,
    ensure_fresh_access_token,
    get_user_connection,
    get_user_oauth_app,
    list_user_oauth_apps,
    poll_device_auth,
    refresh_due_connections,
    register_user_oauth_app,
    start_device_auth,
)

__all__ = [
    "ConnectionMissingError",
    "ConnectionNeedsReauthError",
    "DeviceAuthStart",
    "OAuthError",
    "ProviderConfig",
    "Tokens",
    "UserOAuthApp",
    "UserOAuthConnection",
    "UserOAuthCredential",
    "build_authorize_url",
    "disconnect_user_connection",
    "ensure_fresh_access_token",
    "exchange_code",
    "get_user_connection",
    "get_user_oauth_app",
    "list_user_oauth_apps",
    "poll_device_auth",
    "refresh_access_token",
    "refresh_due_connections",
    "register_user_oauth_app",
    "start_device_auth",
]
