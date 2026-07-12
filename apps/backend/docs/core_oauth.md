# core/oauth

> Generic OAuth 2.0 primitives: authorization-code + refresh for server-side flows, and RFC-8628 device-auth for user-scoped connections.

## Scope

**`service.py` primitives:**

- `build_authorize_url`, `exchange_code`, `refresh_access_token` — server-side authorization-code flow. Pure transport; no persistence or state signing.
- `ProviderConfig` — per-provider OAuth wiring (authorize + token + refresh URLs, auth style, scopes).
- `TokenEndpointSpec` — lighter variant for flows that don't need a full `ProviderConfig` (device-code public-client path).
- `Tokens` — value object wrapping access + optional refresh + id token + `expires_in`.
- `OAuthError` — carries `error_code` (the `"error"` field from RFC-6749/RFC-8628 error bodies) for callers distinguishing device-flow signals (`authorization_pending`, `slow_down`, `access_denied`, `expired_token`) from real failures.

**`user_connections.py` subsystem** — per-user device-code OAuth connections:

- `UserOAuthApp` — frozen dataclass, one per registered provider. Carries device-authorize + token URLs, client credentials, scope list, expiry-source policy, and optional `account_id_extractor`. Providers register at plugin bootstrap via `register_user_oauth_app`.
- `register_user_oauth_app` / `get_user_oauth_app` / `list_user_oauth_apps` — registry CRUD.
- `UserOAuthConnection` — public view of a connection row (no token material).
- `UserOAuthCredential` — token material for the workspace subprocess; unwrap `.access_token` only at the wire boundary.
- `DeviceAuthStart` — result of `start_device_auth`; drives the connect dialog.
- `DeviceAuthStatus` — `Literal["pending", "connected", "denied", "expired", "none"]`.
- `start_device_auth(user_id, provider_id, *, session)` — calls the device-authorize endpoint, upserts `user_oauth_device_sessions`, returns `DeviceAuthStart`.
- `poll_device_auth(user_id, provider_id, *, actor, session)` — one token-endpoint call; handles RFC-8628 flow signals; on grant: encrypts + upserts `user_oauth_connections`, deletes session, emits audit.
- `get_user_connection(user_id, provider_id, *, session)` — returns `UserOAuthConnection | None`.
- `ensure_fresh_access_token(user_id, provider_id, *, session)` — returns `UserOAuthCredential`; raises `ConnectionMissingError` / `ConnectionNeedsReauthError` when reconnect is required.
- `disconnect_user_connection(user_id, provider_id, *, actor, session)` — deletes row + emits audit; returns `bool` (False when not found).
- `ConnectionMissingError`, `ConnectionNeedsReauthError` — raised by `ensure_fresh_access_token`.

Does NOT own: audit fanout logic (uses `core/audit_log.audit`), token refresh scheduling (`domain/pipelines` does the refresh loop via `per_user` skill stage paths), or vendor-specific `auth_json` construction (plugin-owned via `build_auth_json`).

Consumers: `plugins/codex` (registers `UserOAuthApp`, implements `build_auth_json`), `core/oauth/web.py` (HTTP routes), `domain/integrations` + `plugins/github` (authorization-code flow primitives).

## Why / invariants

**`token_auth_style`** — `"basic"` puts `client_id`/`client_secret` in HTTP Basic (Notion-style); `"form"` (default) puts them in the form body (GitHub, OpenAI device-code).

**`ProviderConfig` lives here** because `exchange_code` consumes it. `domain/integrations.types` re-exports it for plugin authors.

**Device-code public client** — `UserOAuthApp.client_secret = None` means `_post_device_authorize` and `_post_token` send only `client_id` (no secret) per RFC-8628 public-client rules. Codex uses this mode.

**Token storage** — all token fields are Fernet-encrypted (`core/secrets`) before persistence. `UserOAuthConnection` carries no token material. `ensure_fresh_access_token` decrypts on the way out and wraps in `UserOAuthCredential` (`SecretStr` fields).

**Audit fanout** — `poll_device_auth` (on grant) and `disconnect_user_connection` emit one `oauth_connection.connected` / `oauth_connection.disconnected` audit row per org the user belongs to, matching the membership-fanout pattern from `core/sessions`.

**Disconnect is delete-only** — no revoke endpoint is called. Revoking at the provider is the user's responsibility.

**DI seams** — `UserOAuthApp.device_authorize_fn` and `token_fn` are `None` in production (module-level functions used). Set to stub callables in tests to avoid network calls without `unittest.mock.patch`.

## Data owned

- `user_oauth_connections` — per-`(user_id, provider_id)` row. `status` ∈ `{'connected', 'needs_reauth'}`. Stores Fernet-encrypted `encrypted_access_token`, `encrypted_refresh_token` (nullable), `encrypted_id_token` (nullable).
- `user_oauth_device_sessions` — per-`(user_id, provider_id)` row; PK is the pair (re-start replaces via upsert). Stores encrypted `device_code`, `user_code`, `verification_url`, `poll_interval_seconds`, `expires_at`.

## HTTP routes (`core/oauth/web.py`)

All routes require a valid session (`require_session`). No org scope — user-scoped (connections are cross-org).

| Method | Path | Action |
|---|---|---|
| `GET` | `/api/user/oauth/connections` | List all registered providers with the caller's status |
| `POST` | `/api/user/oauth/{provider_id}/device-auth/start` | Begin the device-auth handshake |
| `POST` | `/api/user/oauth/{provider_id}/device-auth/poll` | Poll the token endpoint once |
| `DELETE` | `/api/user/oauth/{provider_id}/connection` | Disconnect (delete-only) |

## How it's tested

- `app/core/oauth/test/test_user_connections_service.py` — 12 `@pytest.mark.service` tests covering start/poll/grant/deny/expire/disconnect flows. Uses `UserOAuthApp.device_authorize_fn` / `token_fn` DI seams (no network, no `patch`).
- `app/plugins/codex/test/test_auth_json.py` — 4 unit tests for `build_auth_json` shape + `SecretStr` wrapping.
- `apps/e2e/tests/oauth-connect.spec.ts` — browser connect/disconnect flow against the `fake-openai` peer.
