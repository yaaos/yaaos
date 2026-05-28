# domain/integrations

> Per-(org, provider) hosted-MCP credentials + OAuth lifecycle.

## Scope

Owns: `mcp_credentials` table, OAuth exchange + token storage, per-tool allowlist, hourly health-check loop. Consumes `core/oauth` (protocol), `core/secrets` (encryption), `core/audit_log` (lifecycle events).

Does NOT own: MCP request proxying (`domain/mcp_proxy`), OAuth wire details (`core/oauth`).

## Why / invariants

- **OAuth callback is the only `public_route`** under `/api/mcp-proxy` — the upstream provider can't send the `X-Org-Slug` header; the signed `state` (10m TTL, `itsdangerous`) carries the org_id.
- **Reconnect preserves `allowed_tools`.** Overwriting on reconnect would silently strip the admin's allowlist; the column is untouched on re-exchange.
- **`expires_at < now()` counts as broken_creds** — refresh is deferred; operator reconnects. The proxy returns `-32002` and the reviewer prefixes a warning callout.
- **Hourly health-check** also runs `domain/mcp_proxy.sweep_expired()` — one scheduler, two maintenance tasks.
- **Email dedup:** failure notification fires at most once per 24h per org (`last_failure_notified_at`).

## Data owned

`mcp_credentials` — `(org_id, provider) PK`, encrypted access + refresh tokens, scopes, `allowed_tools`, `enabled`, refresh-status columns.

## How it's tested

- `test/test_service.py` — `connect_callback` / `clear` / `validate` / `update_allowlist` / `list_broken_credentials_for_org` against a stubbed `IntegrationProvider`.
- `test/test_endpoints.py` — every HTTP route (401 / 403 / 404 / success).
- `test/test_scheduler.py` — success keeps `"ok"`; failure flips status + audits + emails; 24h dedup; post-window resend.
- `test/test_broken_creds_chain_service.py` (`@pytest.mark.service`) — cross-module chain: scheduler flips to `"failed"` → reviewer prefixes warning callout.
- E2E: `apps/e2e/tests/integrations-and-multi-org.spec.ts` (broken-creds banner → settings deep-link).
