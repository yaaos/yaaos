# domain/integrations

> Per-(org, provider) hosted-MCP credentials + OAuth lifecycle.

## Purpose

yaaos's concept of "this org has integrated with Linear / Notion." Owns `mcp_credentials`. Consumes `core/oauth` for protocol mechanics + `core/secrets` for at-rest encryption + `core/audit_log` for lifecycle events. Domain-shaped (the concept of an integration is yaaos-specific) while staying free of OAuth wire details.

## Public interface

Exported from `app/domain/integrations/__init__.py`:

- `get(session, org_id, provider) -> McpCredentialRow | None`
- `connect_callback(session, *, provider, code, org_id, redirect_uri, actor, upstream_identity=None) -> McpCredentialRow` — exchanges the OAuth code via `core/oauth`, encrypts both tokens, audits `mcp.<provider>.connected`. Reconnect preserves the existing `allowed_tools`.
- `clear(session, *, org_id, provider, actor) -> bool` — deletes the row, audits `mcp.<provider>.disconnected`.
- `validate(session, *, org_id, provider, actor) -> bool` — calls the provider's `validate(access_token)`. On success flips `last_refresh_status="ok"` + clears `last_refresh_failed_at`; on failure flips `"failed"` + stamps the failure time. Audits `mcp.<provider>.validated`.
- `update_allowlist(session, *, org_id, provider, allowed_tools, actor)` — replaces the per-tool allowlist, audits `mcp.<provider>.allowlist_updated`.
- `IntegrationProvider` Protocol — `provider_id`, `config: ProviderConfig`, `validate(access_token) -> bool`.
- `register_provider(provider)` / `get_provider(provider_id)` / `known_providers()` — bootstrap-time registry that keeps `domain/integrations` free of plugin imports.
- Errors: `IntegrationError`, `ProviderNotRegisteredError`, `IntegrationNotConnectedError`, `BrokenCredentialsError`.

The proxy returns `broken_creds` when the stored access token's `expires_at < now()`; the hourly health-check loop (see below) surfaces the breakage so operators reconnect.

HTTP routes (mounted at `/api/mcp-proxy` with `X-Org-Slug` header):

- `GET /` (`INTEGRATIONS_READ`) — list providers + status (`not_set` / `configured` / `broken`).
- `GET /{provider}/connect` (`INTEGRATIONS_WRITE`) — 303 to the upstream authorize URL with signed `state` (10m TTL, `itsdangerous` over `yaaos_invitation_token_secret`).
- `GET /{provider}/callback` (public_route) — exchange + persist. The OAuth callback path is the only `public_route` exception under `/api/mcp-proxy` because the upstream provider doesn't know our `X-Org-Slug` header — the signed `state` carries the org_id.
- `POST /{provider}/validate` (`INTEGRATIONS_WRITE`) — hit upstream with the stored token.
- `PATCH /{provider}` (`INTEGRATIONS_WRITE`) — update `enabled` and/or `allowed_tools`.
- `DELETE /{provider}` (`INTEGRATIONS_WRITE`) — clear.

## Module architecture

An hourly health-check loop (`scheduler.run_scheduler_loop`) is spawned via the module's `on_startup` hook — it iterates enabled credentials, calls each provider's `validate(access_token)`, flips `last_refresh_status`, audits `mcp.<provider>.token_refresh_failed` on flip-to-failed, and emails the org's Owners (dedup once per 24h via `last_failure_notified_at`). The same loop runs `domain/mcp_proxy.sweep_expired()` so expired review-tokens get reaped without a second scheduler.

## Data owned

- `mcp_credentials` — `(org_id, provider) PK`, encrypted access + refresh tokens, scopes, `allowed_tools`, `enabled`, status columns for the six broken-creds surfaces.

## How it's tested

- `app/domain/integrations/test/test_service.py` round-trips `connect_callback` / `clear` / `validate` / `update_allowlist` against a stubbed `IntegrationProvider` registered into `_REGISTRY`.
- `app/domain/integrations/test/test_endpoints.py` drives every HTTP route (incl. auth triplet: 401 / 403 / 404 / success).
- `app/domain/integrations/test/test_scheduler.py` covers the hourly health-check: success keeps status `"ok"`; failure flips status + audits + emails owners; 24h dedup suppresses repeat emails; post-window resend fires again (also `@pytest.mark.service`).
- **Service test** `app/domain/integrations/test/test_broken_creds_chain_service.py` (`@pytest.mark.service`) drives the cross-module chain end-to-end: scheduler flips a credential to `"failed"`, audits, emails the Owner; the next review's proxy dispatch returns `broken_creds` and the reviewer's `_prefix_broken_creds_warning` composes the GitHub callout.
- E2E for the Owner-connects-Linear/Notion flow ships in `apps/e2e/tests/integrations-and-multi-org.spec.ts` (broken-creds banner → deep-link to Integrations settings page).
