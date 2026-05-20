# domain/mcp_proxy

> Per-review MCP bearer + Streamable-HTTP proxy.

## Purpose

Front-doors every MCP request from a yaaos review. Owns `mcp_review_tokens` (per-review bearer, 2h TTL) and the FastAPI router that speaks MCP Streamable HTTP. Authorizes the JSON-RPC method against the org's per-tool allowlist, forwards to the hosted upstream using the org service-account access token, audit-logs every dispatched method.

## Public interface

Planned (Phase 2):

- `mint_token(review_id) -> raw_token` — sha256-hash persisted; raw returned once.
- `revoke_token(review_id) -> None`
- `dispatch(token, server, json_rpc) -> result` — proxy core.
- `POST /api/mcp/{review_id}/{server}` — FastAPI endpoint (POST + SSE upgrade).

## Module architecture

Skeleton at Phase 0. Phase 2 ships the proxy core + structured JSON-RPC error envelopes for `not_connected`, `broken_creds`, `blocked_by_allowlist`.

## Data owned

- `mcp_review_tokens` — `(token_hash) PK`, `review_id`, `expires_at`, `created_at`. Raw token never persisted.

## How it's tested

Phase 2 tests: mint/lookup/revoke; expired-token TTL rejection; URL-path-vs-token mismatch rejected; concurrent refresh serialization; unconnected provider → `not_connected`; allowlist enforcement.
