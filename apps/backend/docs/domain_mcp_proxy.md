# domain/mcp_proxy

> Per-review MCP bearer + Streamable-HTTP proxy.

## Scope

Owns: `mcp_review_tokens` (per-review bearer, 2h TTL), the FastAPI router for `POST /api/mcp/{review_id}/{server}`, authorization against the org's per-tool allowlist, dispatch to the hosted upstream, audit logging.

## Why / invariants

- **Raw token never stored** — only `sha256(raw)`. `mint_token` returns the raw exactly once.
- **Token is review-scoped**, not user-scoped. `revoke_token(review_id)` runs in a `finally` inside the `with_workspace` block — before tempdir teardown.
- **Read tools always pass; write tools require `allowed_tools` membership.** Claude Code's `--allowed-tools=mcp__<server>__<tool>` is defense-in-depth — the proxy is the actual gate.
- **`expires_at < now()` → `-32002 broken_creds`** — same error code as a failed credential. The reviewer prefixes a warning callout.
- **Audit:** one `mcp.<provider>.dispatched` row per method call. Payload includes `args_hash` (sha256 of canonicalized args) and `result_summary` (compact one-liner — never the full upstream payload).
- Hourly `sweep_expired()` runs on the same scheduler loop as the integrations health-check (`domain/integrations`).

## JSON-RPC error codes

`-32001 not_connected` · `-32002 broken_creds` · `-32003 blocked_by_allowlist` · `-32004 unauthenticated` · `-32005 upstream_error`.

## Data owned

`mcp_review_tokens` — `(token_hash) PK`, `review_id`, `expires_at`, `created_at`.

## How it's tested

- `test/test_service.py` — mint/lookup/revoke/sweep, TTL rejection, mismatched-review revoke, sweep deletes only expired rows.
- `test/test_dispatch.py` — end-to-end dispatch: dispatched audit shape; `not_connected` + `broken_creds` record to broken-creds tracker; `blocked_by_allowlist`; invalid bearer + URL mismatch → 401; mint → revoke → dispatch fails.
