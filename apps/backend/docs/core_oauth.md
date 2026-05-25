# core/oauth

> Generic OAuth 2.0 authorization-code + refresh primitives.

## Purpose

Single home for the OAuth dance. Exposes `build_authorize_url`, `exchange_code`, `refresh_access_token` taking a `ProviderConfig` (URLs + client credentials + scope rules). Knows nothing about yaaos's domain — `domain/integrations` consumes it for hosted-MCP provider plugins and `plugins/github` consumes it for the GitHub OAuth login flow.

## Public interface

- `ProviderConfig` — frozen dataclass: `authorize_url`, `token_url`, `refresh_url`, `mcp_url`, `client_id`, `client_secret`, `scope_separator`, `default_scopes`, `known_read_tools`, `known_write_tools`, `token_auth_style` (`"form"` | `"basic"`). The dataclass home is here because `exchange_code` consumes it; `domain/integrations.types` re-exports for plugin authors who only need to declare it.
- `Tokens` — `access_token`, `refresh_token`, `expires_in`, `scope`, `raw` (full upstream response).
- `build_authorize_url(config, *, state, redirect_uri, scopes=None)` — builds the redirect URL the operator gets shipped to.
- `exchange_code(config, *, code, redirect_uri) -> Tokens` — POSTs the auth-code grant.
- `refresh_access_token(config, *, refresh_token) -> Tokens` — POSTs the refresh grant.
- `OAuthError` — raised on non-2xx + missing `access_token`.

## Module architecture

Pure protocol mechanics; no I/O outside the OAuth round-trip. Callers handle persistence, `state` signing (via `itsdangerous`), and audit emission. `token_auth_style="basic"` swaps the client_id/client_secret onto HTTP Basic for the token endpoint (Notion's quirk); `"form"` (default) puts them in the form body (Linear, GitHub).

## Data owned

None.

## How it's tested

`app/core/oauth/test/` round-trips `build_authorize_url` + `exchange_code` + `refresh_access_token` against a stubbed `httpx.AsyncClient`. Integration coverage with the real fake upstream lives in `app/domain/integrations/test/`.
