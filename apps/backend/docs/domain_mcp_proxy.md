# domain/mcp_proxy

> Per-review MCP bearer + Streamable-HTTP proxy.

## Purpose

Front-doors every MCP request from a yaaos review. Owns `mcp_review_tokens` (per-review bearer, 2h TTL) and the FastAPI router that speaks MCP Streamable HTTP. Authorizes the JSON-RPC method against the org's per-tool allowlist, forwards to the hosted upstream using the org service-account access token, audit-logs every dispatched method.

## Public interface

- `mint_token(review_id, *, session=None) -> str` — issues a 32-byte URL-safe random bearer; persists only `sha256(raw)` with `expires_at = created_at + 2h`. Raw returned exactly once.
- `lookup_token(raw_token, *, session=None) -> McpToken | None` — sha256 the input, look up by primary key, return None if expired or missing. `McpToken` is a Pydantic value object with `review_id` and `expires_at`.
- `revoke_token(review_id, *, session=None) -> int` — drop every token row for a review. Reviewer calls this before workspace teardown.
- `sweep_expired(*, session=None) -> int` — periodic cleanup. Runs on the same hourly loop as the integrations health-check.
- `record_broken_creds(review_id, provider)` / `consume_broken_creds(review_id) -> set[str]` — process-local tracker the proxy writes on every `not_connected` / `broken_creds` rejection and the reviewer drains at review-end to prefix the PR summary with a yellow warning callout.
- `hash_token(raw_token) -> str` — sha256 hex of a raw bearer. Used by callers that need to verify a stored hash against a known raw token.
- `get_token_by_hash(token_hash, *, session) -> McpReviewTokenRow | None` — targeted read for asserting on the persisted token row after minting.
- `POST /api/mcp/{review_id}/{server}` — the FastAPI router (public_route, bearer-authenticated). Handles JSON-RPC over POST; SSE upgrade not needed because the fake stack + production hosted MCPs return plain JSON-RPC.

## Module architecture

JSON-RPC application errors use the `-32000..-32099` range with a string `data.code`:

- `-32001 not_connected` — no `mcp_credentials` row, or `enabled=False`.
- `-32002 broken_creds` — row exists with `last_refresh_status="failed"`, OR access token's `expires_at < now()` (refresh deferred; operator reconnects).
- `-32003 blocked_by_allowlist` — write tool not in the row's `allowed_tools`.
- `-32004 unauthenticated` — invalid bearer or URL-path-vs-token-review_id mismatch.
- `-32005 upstream_error` — upstream HTTP non-2xx or transport error.

Authorization flow for `tools/call`: read tools (in `config.known_read_tools`) are always allowed; write tools (in `config.known_write_tools`) must appear in `credential.allowed_tools` to forward. The proxy is the actual gate — Claude Code's `--allowed-tools=mcp__<server>__<tool>` is defense-in-depth.

Audit: one `mcp.<provider>.dispatched` row per JSON-RPC method call (no batching). Payload: `provider`, `method`, `tool`, `args_hash` (sha256 of canonicalized arguments), `result_summary` (compact one-line — never the full upstream payload, which may contain customer data), `upstream_account="org_service_account"`. Actor is `Actor.system()`.

The reviewer integration: `domain/reviewer.queue._build_mcp_payload` mints a token per review_job, threads it via `ReviewContext.agent_config["mcp"]` into `plugins/claude_code`, which materializes `.mcp.json` in the workspace. `revoke_token(review_id)` runs in a `finally` inside the `with_workspace` block — before the tempdir tears down.

## Data owned

- `mcp_review_tokens` — `(token_hash) PK`, `review_id`, `expires_at`, `created_at`. Raw token never persisted.

## How it's tested

- `app/domain/mcp_proxy/test/test_service.py` — mint/lookup/revoke/sweep + TTL rejection + mismatched-review revoke + sweep deletes only expired rows.
- `app/domain/mcp_proxy/test/test_dispatch.py` — end-to-end `POST /api/mcp/{review_id}/{server}` against a stubbed upstream + stubbed `IntegrationProvider`: dispatched audit shape; `not_connected` + `broken_creds` both record to the per-review broken-creds tracker; `blocked_by_allowlist` for write tools; invalid bearer + URL-path-vs-token mismatch both return 401; mint → revoke → dispatch fails.
