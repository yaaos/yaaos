"""Generic OAuth 2.0 authorization-code + refresh primitives.

Three top-level functions:

- `build_authorize_url(config, state, scopes, redirect_uri)` — return the URL the
  user agent should be 302'd to. Pure string-building; no I/O.
- `exchange_code(config, code, redirect_uri)` — POST the token endpoint. Returns
  a `Tokens` value object (access + optional refresh + expires_in + scope).
- `refresh_access_token(config, refresh_token)` — POST the refresh endpoint.
  Returns the same `Tokens` shape; many providers rotate refresh tokens, so
  callers persist the new refresh value when it changes.

Provider-specific quirks (scope separator, token-endpoint auth style: form vs
HTTP Basic) live in `ProviderConfig`. Anything beyond the OAuth dance —
persistence, signing of `state`, audit emission — is the caller's job.

Internal primitive `_post_token` now accepts `TokenEndpointSpec` so the
user-connection subsystem (public-client device-code flow) can reuse the
transport without a fake `ProviderConfig`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import structlog
from pydantic import SecretStr

log = structlog.get_logger("core.oauth")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """OAuth + MCP wiring for one upstream provider.

    Provider plugins fill this in at bootstrap. Env-var overrides
    (`LINEAR_OAUTH_AUTHORIZE_URL` etc.) let the test compose swap in the
    local fakes; production defaults are the real upstream URLs.

    Lives here in `core/oauth` rather than `domain/integrations` because
    `core/oauth.exchange_code` consumes it — `core` can't import `domain`.
    """

    authorize_url: str
    token_url: str
    refresh_url: str
    mcp_url: str
    client_id: str
    client_secret: SecretStr
    scope_separator: str  # " " for most; commas for some
    default_scopes: tuple[str, ...]
    known_read_tools: tuple[str, ...]
    known_write_tools: tuple[str, ...]
    # "form" (default — body-encoded client creds) or "basic" (HTTP Basic, à la Notion).
    token_auth_style: Literal["form", "basic"] = "form"


@dataclass(frozen=True, slots=True)
class TokenEndpointSpec:
    """Narrow transport spec shared by both ProviderConfig callers and the
    device-code / user-connection subsystem.

    `client_secret=None` indicates a public client — no secret is sent on
    token calls (device-code grant; some PKCE apps). `token_auth_style`
    drives whether the secret travels in the form body ("form", default) or
    as HTTP Basic ("basic", à la Notion).
    """

    url: str
    client_id: str
    client_secret: SecretStr | None  # None → public client
    token_auth_style: Literal["form", "basic"]


_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class Tokens:
    """OAuth token-endpoint response, normalized.

    `refresh_token` may be `None` for providers that don't issue refresh
    tokens. `expires_in` is seconds-from-now; the caller turns it into an
    absolute `expires_at` when persisting. `id_token` is `None` for providers
    that don't issue an OIDC id_token (only codex's device-auth path captures
    it via `UserOAuthApp.capture_id_token`).
    """

    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_in: int
    scope: str
    raw: dict[str, Any]
    id_token: SecretStr | None = None


class OAuthError(RuntimeError):
    """Token-endpoint or transport failure. Caller surfaces to user.

    `error_code` carries the OAuth error body's `error` field (e.g.
    ``"authorization_pending"``, ``"access_denied"``) when the provider
    returned a structured error JSON. `None` for transport errors or
    non-JSON responses.
    """

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


def build_authorize_url(
    config: ProviderConfig,
    *,
    state: str,
    redirect_uri: str,
    scopes: list[str] | None = None,
) -> str:
    """Build the URL we 302 the user to. `scopes=None` uses `config.default_scopes`."""
    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "scope": config.scope_separator.join(scopes or config.default_scopes),
    }
    sep = "&" if "?" in config.authorize_url else "?"
    return f"{config.authorize_url}{sep}{urlencode(params)}"


async def exchange_code(
    config: ProviderConfig,
    *,
    code: str,
    redirect_uri: str,
) -> Tokens:
    """Exchange an authorization code for tokens."""
    spec = _spec_from_config(config, refresh=False)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    return await _post_token(spec, data)


async def refresh_access_token(
    config: ProviderConfig,
    *,
    refresh_token: SecretStr,
) -> Tokens:
    """Exchange a refresh token for a new access token (+ possibly rotated refresh)."""
    spec = _spec_from_config(config, refresh=True)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token.get_secret_value(),
    }
    return await _post_token(spec, data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _spec_from_config(config: ProviderConfig, *, refresh: bool) -> TokenEndpointSpec:
    return TokenEndpointSpec(
        url=config.refresh_url if refresh else config.token_url,
        client_id=config.client_id,
        client_secret=config.client_secret,
        token_auth_style=config.token_auth_style,
    )


async def _post_token(
    spec: TokenEndpointSpec,
    data: dict[str, str],
) -> Tokens:
    """POST the token endpoint described by `spec`.

    On a structured error response (non-200 with a JSON body containing an
    `error` field), raises `OAuthError` with `error_code` set to that field
    value so callers can distinguish device-code flow states
    (``"authorization_pending"``, ``"slow_down"``, etc.) from real failures.
    """
    url = spec.url
    headers: dict[str, str] = {"Accept": "application/json"}

    # Provider-specific client-auth style.
    if spec.client_secret is None:
        # Public client — only send client_id, no secret.
        data = {**data, "client_id": spec.client_id}
    elif spec.token_auth_style == "basic":
        creds = f"{spec.client_id}:{spec.client_secret.get_secret_value()}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(creds).decode()
    else:
        data = {
            **data,
            "client_id": spec.client_id,
            "client_secret": spec.client_secret.get_secret_value(),
        }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as http:
            resp = await http.post(url, data=data, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("oauth.transport_error", url=url, error=str(exc))
        raise OAuthError(f"transport error: {exc}") from exc

    # Try to parse the body regardless of status so we can surface error_code.
    body: dict[str, Any] = {}
    try:
        body = resp.json()
    except ValueError:
        pass

    if resp.status_code != 200:
        error_code = body.get("error") if body else None
        log.warning(
            "oauth.non_200",
            url=url,
            status=resp.status_code,
            error_code=error_code,
            body=resp.text[:300],
        )
        raise OAuthError(
            f"token endpoint returned {resp.status_code}",
            error_code=error_code,
        )

    access = body.get("access_token")
    if not access:
        raise OAuthError("missing access_token in response")
    refresh = body.get("refresh_token")
    id_token = body.get("id_token")
    return Tokens(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh) if refresh else None,
        expires_in=int(body.get("expires_in") or 3600),
        scope=body.get("scope") or "",
        raw=body,
        id_token=SecretStr(id_token) if id_token else None,
    )


async def _post_device_authorize(
    *,
    device_authorize_url: str,
    client_id: str,
    scopes: tuple[str, ...],
    scope_separator: str,
) -> dict[str, Any]:
    """POST the RFC-8628 device-authorization endpoint.

    Returns the raw response dict; caller extracts `device_code`, `user_code`,
    `verification_uri`, `expires_in`, and optional `interval`.
    """
    data = {
        "client_id": client_id,
        "scope": scope_separator.join(scopes),
    }
    headers: dict[str, str] = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as http:
            resp = await http.post(device_authorize_url, data=data, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("oauth.device_authorize.transport_error", url=device_authorize_url, error=str(exc))
        raise OAuthError(f"device-authorize transport error: {exc}") from exc

    if resp.status_code != 200:
        log.warning(
            "oauth.device_authorize.non_200",
            url=device_authorize_url,
            status=resp.status_code,
            body=resp.text[:300],
        )
        raise OAuthError(f"device-authorize endpoint returned {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise OAuthError(f"non-json device-authorize response: {exc}") from exc
