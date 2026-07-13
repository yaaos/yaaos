# domain/mcp_server

> Inbound OAuth 2.1 authorization server + MCP tool host for local coding agents.

Not to be confused with [`domain/mcp_proxy`](domain_mcp_proxy.md), which proxies outbound MCP calls from Claude Code to external tool servers. This module is the *inbound* surface: it issues OAuth tokens to agents that want to call back into yaaos as an MCP server.

## Scope

Owns: four OAuth tables (`mcp_oauth_clients`, `mcp_auth_codes`, `mcp_access_tokens`, `mcp_refresh_tokens`), the FastAPI routes for the RFC 8414 discovery document + RFC 7591 client registration + authorize + token endpoints, the FastMCP `mcp` server instance and its `find_ticket` tool, bearer token lifecycle helpers.

Does **not** own: the session cookie (reads it from `core/identity` / `core/sessions`); org membership (reads from `core/tenancy`); the outer FastAPI app's lifespan (the composition root chains the FastMCP lifespan in `app/web.py`).

## Why / invariants

- **Raw token never stored** — `secrets.token_urlsafe(32)` returned once; sha256 stored. Same discipline as `domain/mcp_proxy` and `core/sessions`.
- **PKCE S256 required** — `code_challenge_method=S256` is enforced at the authorize endpoint and verified at token exchange. Plain PKCE is rejected.
- **Public clients only** — `token_endpoint_auth_method=none`. No client secret; PKCE is the proof-of-possession mechanism.
- **Org locked at consent time** — the user picks the org on the consent form; `McpPrincipal.org_id` never changes on refresh rotation.
- **Role resolved live** — `authenticate()` calls `get_member_role(session, org_id=..., user_id=...)` on every inbound bearer check so freshly demoted members are rejected without waiting for token rotation.
- **Access token TTL** — 8 hours (`ACCESS_TOKEN_TTL`). Refresh token TTL — 4 weeks (`REFRESH_TOKEN_TTL`).
- **Hourly sweep** — `mcp_server_token_sweep` (`@scheduled`, cron `0 * * * *`) drops expired access + refresh rows.
- **FastMCP lifespan** — `StreamableHTTPSessionManager.run()` is single-use per instance. The composition root (`app/web.py`) creates a fresh `mcp.http_app()` on each lifespan start via `_MCPProxy`; tests that restart the ASGI lifespan each get a virgin session manager.
- **Tool auth bridge** — `YaaosTokenVerifier` implements FastMCP's `TokenVerifier`; it opens a DB session, calls `authenticate()`, and serialises the resulting `McpPrincipal` into `AccessToken.claims`. Tool handlers reconstruct the principal via `_get_principal()` without a second DB hit.

## Public interface

Exported from `__init__.py`:

- `ACCESS_TOKEN_TTL`, `REFRESH_TOKEN_TTL` — token lifetime constants.
- `McpAuthError` — raised by `authenticate()` on any failure.
- `McpPrincipal` — frozen Pydantic model: `user_id`, `org_id`, `role`.
- `authenticate(bearer, *, session)` — verifies an inbound MCP bearer; returns `McpPrincipal` or raises `McpAuthError`.
- `revoke_tokens_for_user(user_id, *, session)` — deletes all token rows for a user.
- `mcp` — the `FastMCP` server instance; mounted by the composition root.

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

1. **Client registration** — `POST /api/mcp-server/register` with `{client_name, redirect_uris}` → mints a `client_id` UUID, inserts `McpOAuthClientRow`, returns `{client_id, ...}`. No prior auth required (RFC 7591 §3.2).
2. **Authorization** — `GET /api/mcp-server/authorize?client_id=…&response_type=code&redirect_uri=…&code_challenge=…[&state=…]` → reads `yaaos_session` cookie via `core/identity.find_session_by_hash`; unauthenticated → redirect to `/login?next=…`; authenticated → render consent HTML with org picker.
3. **Consent** — `POST /api/mcp-server/authorize/consent` (form POST) → verifies session + client + redirect_uri + org membership; mints one-time auth code; redirects to `redirect_uri?code=…[&state=…]`.
4. **Token exchange** — `POST /api/mcp-server/token` with `grant_type=authorization_code, code=…, code_verifier=…, redirect_uri=…` → verifies PKCE S256; consumes code (one-time); mints access + refresh tokens; returns `{access_token, token_type, expires_in, refresh_token}`.
5. **Token refresh** — `POST /api/mcp-server/token` with `grant_type=refresh_token` → `rotate_refresh_token` atomically deletes old refresh token and mints a new pair.
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

- `test/test_oauth_service.py` (`@pytest.mark.service`) — full OAuth flow: registration, consent page, consent form with PKCE, code exchange, refresh rotation, token expiry, client_id mismatch, wrong PKCE verifier, code reuse.
- `test/test_tools_service.py` (`@pytest.mark.service`) — FastMCP `initialize` with valid bearer; bad bearer → 401; `find_ticket` with unknown branch → null result; `find_ticket` with seeded ticket → found; bad bearer on tool call → 401; full create→attach→inspect loop; role-floor check (BUILDER can write).
