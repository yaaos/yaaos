# domain/integrations

> Per-(org, provider) hosted-MCP credentials + OAuth lifecycle.

## Scope

Owns: `mcp_credentials` table, OAuth exchange + token storage, per-tool allowlist, hourly health-check loop, `GET /api/integrations/broken-summary` (cross-org broken-creds surface for the SPA). Consumes `core/oauth` (protocol), `core/secrets` (encryption), `core/audit_log` (lifecycle events), `core/tenancy` (membership lookup for broken-summary).

Does NOT own: MCP request proxying (`domain/mcp_proxy`), OAuth wire details (`core/oauth`).

## Why / invariants

- **OAuth callback is the only `public_route`** under `/api/mcp-proxy` — the upstream provider can't send the `X-Org-Slug` header; the signed `state` (10m TTL, `itsdangerous`) carries the org_id.
- **Reconnect preserves `allowed_tools`.** Overwriting on reconnect would silently strip the admin's allowlist; the column is untouched on re-exchange.
- **Secret material rides a separate VO.** The `McpCredential` metadata VO carries no token; the encrypted access token is fetched only via `get_secret` (returns `McpCredentialSecret`) at the one call site that decrypts (`domain/mcp_proxy`), so a stray `model_dump()` of the metadata VO can't leak ciphertext.
- **`expires_at < now()` counts as broken_creds** — refresh is deferred; operator reconnects. The proxy returns `-32002` and the reviewer prefixes a warning callout.
- **Hourly health-check** — one credential pass per tick; sweep of expired `mcp_review_tokens` is `domain/mcp_proxy`'s own responsibility (see [`domain_mcp_proxy.md`](domain_mcp_proxy.md)).
- **Email dedup:** failure notification fires at most once per 24h per org (`last_failure_notified_at`).
- **`GET /api/integrations/broken-summary`** — cookie-auth (`public_route`); no `X-Org-Slug`. Reads the caller's memberships via `core/tenancy`, then queries `mcp_credentials` directly for each Admin/Owner org. Response: `{ orgs: [{ org_id, broken_integrations: [{ provider }] }] }`. Builders always see empty lists. This keeps broken-credential data in the integrations module rather than on `/api/auth/me`.

## Data owned

`mcp_credentials` — `(org_id, provider) PK`, encrypted access + refresh tokens, scopes, `allowed_tools`, `enabled`, refresh-status columns.

## How it's tested

- `test/test_service.py` — `connect_callback` / `clear` / `validate` / `update_allowlist` / `list_broken_credentials_for_org` against a stubbed `IntegrationProvider`.
- `test/test_endpoints.py` — every HTTP route (401 / 403 / 404 / success), including `GET /api/integrations/broken-summary` (unauthenticated 401, empty-when-no-broken-creds, returns broken creds for Admins, empty for Builders).
- `test/test_scheduler.py` — success keeps `"ok"`; failure flips status + audits + emails; 24h dedup; post-window resend.
- `test/test_broken_creds_chain_service.py` (`@pytest.mark.service`) — cross-module chain: scheduler flips to `"failed"` → reviewer prefixes warning callout.
- E2E: `apps/e2e/tests/integrations-and-multi-org.spec.ts` (broken-creds banner → settings deep-link).
