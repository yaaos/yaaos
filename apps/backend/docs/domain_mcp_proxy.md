# domain/mcp_proxy

> Per-review MCP bearer + Streamable-HTTP proxy.

## Scope

Owns: `mcp_review_tokens` (per-review bearer, 2h TTL), the FastAPI router for `POST /api/mcp/{review_id}/{server}`, authorization against the org's per-tool allowlist, dispatch to the hosted upstream, audit logging.

## Why / invariants

- **Raw token never stored** — only `sha256(raw)`. `mint_token` returns the raw exactly once.
- **Token is review-scoped**, not user-scoped. `revoke_token(review_id)` is called by the reviewer's cleanup step — before the workspace is closed.
- **`mint_token(review_id, *, org_id, session)` stores org on the token row.** The proxy reads `org_id` from `McpToken.org_id` without a round-trip into `domain/reviewer`. The `reviewer → mcp_proxy` direction is the only live edge; the former back-edge is gone.
- **Read tools always pass; write tools require `allowed_tools` membership.** Claude Code's `--allowed-tools=mcp__<server>__<tool>` is defense-in-depth — the proxy is the actual gate.
- **`expires_at < now()` → `-32002 broken_creds`** — same error code as a failed credential. The reviewer prefixes a warning callout.
- **Audit:** one `mcp.<provider>.dispatched` row per method call. Payload includes `args_hash` (sha256 of canonicalized args) and `result_summary` (compact one-liner — never the full upstream payload).
- **Own hourly sweep:** runs as a `@scheduled` worker task (`mcp_review_token_sweep`, cron `0 * * * *`). Exactly one worker pod enqueues each slot. `sweep_expired` is a backstop GC — expiry is enforced at `lookup_token`, so a slow sweep only delays deletion of dead rows.

## JSON-RPC error codes

`-32001 not_connected` · `-32002 broken_creds` · `-32003 blocked_by_allowlist` · `-32004 unauthenticated` · `-32005 upstream_error`.

## Data owned

`mcp_review_tokens` — `(token_hash) PK`, `review_id`, `org_id`, `expires_at`, `created_at`. `org_id` is stored at mint time so the proxy reads tenancy directly from the token row — no back-lookup into the reviewer is needed.

## How it's tested

- `test/test_service.py` — mint/lookup/revoke/sweep, TTL rejection, mismatched-review revoke, sweep deletes only expired rows; `test_mint_token_stores_org_id` asserts `org_id` is persisted and surfaced on `McpToken`.
- `test/test_review_token_sweep_scheduled_service.py` (`@pytest.mark.service`) — broker-registration guard for `mcp_review_token_sweep` task; sweep body deletes expired rows end-to-end.
- `test/test_dispatch.py` — end-to-end dispatch: dispatched audit shape; `not_connected` + `broken_creds` record to broken-creds tracker; `blocked_by_allowlist`; invalid bearer + URL mismatch → 401; mint → revoke → dispatch fails; `test_proxy_reads_org_from_token_row` asserts the proxy resolves tenancy from the token row.
