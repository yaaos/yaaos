# domain/mcp_server

> Inbound OAuth 2.1 authorization server + MCP tool host for local coding agents.

Not to be confused with [`domain/mcp_proxy`](domain_mcp_proxy.md), which proxies outbound MCP calls from Claude Code to external tool servers. This module is the *inbound* surface: it issues OAuth tokens to agents that want to call back into yaaos as an MCP server.

## Scope

Owns: four OAuth tables (`mcp_oauth_clients`, `mcp_auth_codes`, `mcp_access_tokens`, `mcp_refresh_tokens`), the FastAPI routes for the RFC 8414 discovery document + RFC 7591 client registration + authorize + token endpoints, the FastMCP server instance (private to `tools.py`) and its tool roster, `mount(app)` (ASGI sub-app mount + lifespan chaining, `asgi.py`), bearer token lifecycle helpers.

Does **not** own: the session cookie (reads it from `core/identity` / `core/sessions`); org membership (reads from `core/tenancy`); the outer FastAPI app object (the composition root passes it to `mount()` after `create_app()`).

## Why / invariants

- **Raw token never stored** — `secrets.token_urlsafe(32)` returned once; sha256 stored. Same discipline as `domain/mcp_proxy` and `core/sessions`.
- **PKCE S256 required** — `code_challenge_method=S256` is enforced at the authorize endpoint and verified at token exchange. Plain PKCE is rejected.
- **Public clients only** — `token_endpoint_auth_method=none`. No client secret; PKCE is the proof-of-possession mechanism.
- **Consent page escapes everything** — `_consent_html` HTML-escapes every interpolated value (`html.escape(..., quote=True)`): `client_name` is stored verbatim by the unauthenticated `/register` endpoint and `state` is a query param — both attacker-controlled.
- **Registration metadata validated hard** — `/register` is unauthenticated, so `_RegisterRequest` caps `client_name` (≤256, printable) and `redirect_uris` (≤5, each ≤2048, `https://` only with an `http://localhost` / `http://127.0.0.1` loopback carve-out per RFC 8252 §7.3). Violations return HTTP 400 `invalid_client_metadata` (RFC 7591 §3.2.2 shape, not FastAPI's 422 — the body is parsed manually).
- **Registration rate-limited per source IP** — `rate_limit.py` runs two sliding windows on every `/register` call (burst: 3 / 60s; sustained: 10 / 3600s), both backed by [`core/redis.sliding_window_hit`](core_redis.md) under the `rl:mcp_register:` key prefix. Both must pass. Registration is a once-per-client-install action, so the limits are generous even for an office behind one NAT address while keeping bulk row-creation impractical. A violation returns HTTP 429 `too_many_requests` with `Retry-After: <violated window's seconds>`, in the same `{error, error_description}` envelope as the 400. The check runs before body parsing — a caller cannot buy budget by sending garbage. Requests with no client address (proxy stripping / in-process harness) skip the check.
- **Org locked at consent time** — the user picks the org on the consent form; `McpPrincipal.org_id` never changes on refresh rotation.
- **Failed refresh validation never consumes the token** — `rotate_refresh_token` checks existence, expiry, AND `client_id` match before deleting the old row (RFC 6749 §5.2); a mismatched attempt leaves the legitimate holder's token usable.
- **Tokens die with the user** — `revoke_tokens_for_user` is registered as a `core/identity` user-deletion hook at import time (the token tables carry no FK to `users`), so `core/identity.delete_user` revokes MCP bearers in the same transaction.
- **Role resolved live** — `authenticate()` calls `get_member_role(session, org_id=..., user_id=...)` on every inbound bearer check so freshly demoted members are rejected without waiting for token rotation.
- **Access token TTL** — 8 hours (`ACCESS_TOKEN_TTL`). Refresh token TTL — 4 weeks (`REFRESH_TOKEN_TTL`).
- **Hourly sweep** — `mcp_server_token_sweep` (`@scheduled`, cron `0 * * * *`) drops expired access + refresh rows, then prunes `mcp_oauth_clients` rows older than `UNUSED_CLIENT_MAX_AGE` (7 days) that no auth code, access token, or refresh token references. A client with any live token is kept regardless of age; abandoned registrations (the unauthenticated `/register` creates a row before any authorize) do not accumulate. Token deletion runs first in the same transaction, so a client whose last token just expired is prunable in the same pass.
- **FastMCP lifespan** — `StreamableHTTPSessionManager.run()` is single-use per instance. `mount(app)` (`asgi.py`) chains the FastMCP lifespan into the app's `lifespan_context` (Starlette does not propagate lifespan to mounted sub-apps) and creates a fresh `mcp.http_app()` on each lifespan start via an ASGI proxy; tests that restart the ASGI lifespan each get a virgin session manager.
- **Tool auth bridge** — `YaaosTokenVerifier` implements FastMCP's `TokenVerifier`; it opens a DB session, calls `authenticate()`, and serialises the resulting `McpPrincipal` into `AccessToken.claims`. Tool handlers reconstruct the principal via `_get_principal()` without a second DB hit.

## Public interface

Exported from `__init__.py`:

- `ACCESS_TOKEN_TTL`, `REFRESH_TOKEN_TTL` — token lifetime constants.
- `McpAuthError` — raised by `authenticate()` on any failure.
- `McpPrincipal` — frozen Pydantic model: `user_id`, `org_id`, `role`.
- `authenticate(bearer, *, session)` — verifies an inbound MCP bearer; returns `McpPrincipal` or raises `McpAuthError`.
- `revoke_tokens_for_user(user_id, *, session)` — deletes all token rows for a user; also registered as a `core/identity` user-deletion hook at import time.
- `mount(app)` — mounts the FastMCP sub-app at `/api/mcp-server/mcp` + the `/.well-known` discovery route and chains the FastMCP lifespan; called once by the composition root after `create_app()`. The `FastMCP` instance itself is private to `tools.py` (Cardinal rule — never export the instance).

OAuth routes registered via `oauth_web.py` side-effect import:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/.well-known/oauth-authorization-server` | RFC 8414 discovery |
| `POST` | `/api/mcp-server/register` | RFC 7591 dynamic client registration |
| `GET` | `/api/mcp-server/authorize` | Session-gated consent page |
| `POST` | `/api/mcp-server/authorize/consent` | Form submission → redirect + code |
| `POST` | `/api/mcp-server/token` | `authorization_code` + PKCE or `refresh_token` exchange |

FastMCP sub-app mounted at `/api/mcp-server/mcp` (Streamable HTTP transport).

## Data owned

- `mcp_oauth_clients` — dynamic client registrations (`client_id` UUID PK, `client_name`, `redirect_uris` JSONB).
- `mcp_auth_codes` — one-time auth codes (`code_hash` TEXT PK; FK → `mcp_oauth_clients`; `user_id`, `org_id`, `code_challenge`, `redirect_uri`, `expires_at`).
- `mcp_access_tokens` — opaque bearer tokens (`token_hash` TEXT PK; FK → `mcp_oauth_clients`; `user_id`, `org_id`, `expires_at`). Index: `idx_mcp_access_tokens_user(user_id)`.
- `mcp_refresh_tokens` — rotation tokens (`token_hash` TEXT PK; FK → `mcp_oauth_clients`; `user_id`, `org_id`, `expires_at`). Index: `idx_mcp_refresh_tokens_user(user_id)`.

Migration: `c6d7e8f9a0b1_add_mcp_server_tables.py`.

## Core flows

1. **Client registration** — `POST /api/mcp-server/register` with `{client_name, redirect_uris}` → per-IP rate check (429 `too_many_requests` + `Retry-After` when a window is full) → validates metadata (caps + scheme allowlist; 400 `invalid_client_metadata` on violation), mints a `client_id` UUID, inserts `McpOAuthClientRow`, returns `{client_id, ...}`. No prior auth required (RFC 7591 §3.2).
2. **Authorization** — `GET /api/mcp-server/authorize?client_id=…&response_type=code&redirect_uri=…&code_challenge=…[&state=…]` → reads `yaaos_session` cookie via `core/identity.find_session_by_hash`; unauthenticated → redirect to `/login?next=…`; authenticated → render consent HTML with org picker.
3. **Consent** — `POST /api/mcp-server/authorize/consent` (form POST) → verifies session + client + redirect_uri + org membership; mints one-time auth code; redirects to `redirect_uri?code=…[&state=…]`.
4. **Token exchange** — `POST /api/mcp-server/token` with `grant_type=authorization_code, code=…, code_verifier=…, redirect_uri=…` → verifies PKCE S256; consumes code (one-time); mints access + refresh tokens; returns `{access_token, token_type, expires_in, refresh_token}`.
5. **Token refresh** — `POST /api/mcp-server/token` with `grant_type=refresh_token` → `rotate_refresh_token` validates existence + expiry + `client_id` match, then atomically deletes the old refresh token and mints a new pair. Any validation failure returns `invalid_grant` without consuming the token.
6. **MCP tool call** — FastMCP sub-app receives bearer in `Authorization` header → `YaaosTokenVerifier.verify_token` → `authenticate()` → principal in `AccessToken.claims` → tool handler reads via `_get_principal()`.

## MCP tools

All tools read org from `McpPrincipal` (set at consent time) — never from tool arguments. Write tools (`create_ticket`, `add_attachment`, `start_run`) require builder+ role. Tool-level errors are JSON-RPC error payloads (not HTTP error codes): `-32001` not found, `-32002` constraint violation (e.g. run in flight), `-32004` auth / role failure, `-32602` invalid argument.

| Tool | Wraps | In | Out |
|---|---|---|---|
| `find_ticket` | `tickets.get_by_branch` | `{branch_name}` | `{ticket_id, title, status}` (null fields when not found) |
| `create_ticket` ✏ | `tickets.create_from_manual` | `{title, repo_external_id, branch_name?, idempotency_key?}` | `{ticket_id, created}` |
| `add_attachment` ✏ | `attachments.add_attachment` | `{ticket_id, filename, body, note?}` | `{attachment_id, produced_by_skill, artifact_type}` |
| `start_run` ✏ | `pipelines.start_manual_run` | `{ticket_id, pipeline_id, prompt?, replace_in_flight?}` | `{run_id}` |
| `get_ticket` | `tickets.get` | `{ticket_id}` | `{id, title, status, branch_name, repo_external_id, created_at}` |
| `get_run_overview` | `pipelines.get_run_overview` | `{ticket_id}` | RunOverview (`paused \| in_flight \| terminal`) or null |
| `list_findings` | `findings.list_open_for_ticket` | `{ticket_id}` | `[{id, handle, severity, body, file, line}]` |
| `list_artifacts` | `artifacts.list_for_ticket` | `{ticket_id}` | `[{stage_name, versions: [{id, version, is_final, created_at, adopted_from_attachment_id}]}]` |
| `get_artifact` | `artifacts.get` | `{artifact_id}` | `{id, stage_name, version, is_final, body, created_at, adopted_from_attachment_id}` |
| `list_attachments` | `attachments.list_attachments` | `{ticket_id}` | `[{id, filename, produced_by_skill, artifact_type, note, attached_at}]` |
| `list_pipelines` | `pipelines.list_pipelines` | `{}` | `[{id, name, description}]` |

`get_run_overview` and `list_pipelines` require `org_id_var` + `user_id_var` contextvars (set internally by `_mcp_tool_context` before the wrapped service call).

## How it's tested

- `test/test_oauth_service.py` (`@pytest.mark.service`) — full OAuth flow: registration (including metadata caps + `javascript:`/non-loopback-`http` redirect-URI rejection), consent page (including HTML-escaping of `client_name` and `state`), consent form with PKCE, code exchange, refresh rotation (including mismatched-client non-consumption), token expiry, wrong PKCE verifier, code reuse, user-deletion hook revocation, sweep (expired-token deletion + unused-client prune: old-unused deleted, recent kept, old-with-live-token kept). Every test's HTTP client carries a unique source IP so the per-IP registration windows never couple tests.
- `test/test_register_rate_limit_service.py` (`@pytest.mark.service`) — burst window trips on the 4th registration in a minute; sustained window trips on the 11th in an hour; a different IP is unaffected; a request with no client address skips the check. Both assert the 429's `Retry-After`. `test/conftest.py` flushes the `rl:mcp_register:` keys before each test — Redis is not rolled back between tests like Postgres is.
- `test/test_tools_service.py` (`@pytest.mark.service`) — FastMCP `initialize` with valid bearer; bad bearer → 401; `find_ticket` with unknown branch → null result; `find_ticket` with seeded ticket → found; bad bearer on tool call → 401; full create→attach→inspect loop; role-floor check (BUILDER can write).
