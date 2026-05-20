# domain/integrations

> Per-(org, provider) hosted-MCP credentials + OAuth lifecycle.

## Purpose

yaaos's concept of "this org has integrated with Linear / Notion." Owns `mcp_credentials`. Consumes `core/oauth` for protocol mechanics + `core/secrets` for at-rest encryption + `core/audit_log` for lifecycle events. Domain-shaped (the concept of an integration is yaaos-specific) while staying free of OAuth wire details.

## Public interface

Planned (Phase 1+):

- `connect_start(org_id, provider, user_initiating) -> redirect_url`
- `connect_callback(provider, code, state) -> credential_row`
- `get(org_id, provider) -> credential_row | None`
- `refresh(org_id, provider)` — advisory-lock-guarded per `(org_id, provider)`
- `clear(org_id, provider)` — deletes the row + audits
- `validate(org_id, provider)` — calls the provider plugin's validator
- `update_allowlist(org_id, provider, allowed_tools)`

Provider plugins register their `IntegrationProvider` (`ProviderConfig` + `validate` callable) via `register_provider(...)` at bootstrap so `domain/integrations` stays free of plugin imports.

## Module architecture

Skeleton at Phase 0; the service surface and HTTP routes land in Phase 1 (Linear) and Phase 1b (Notion).

## Data owned

- `mcp_credentials` — `(org_id, provider) PK`, encrypted access + refresh tokens, scopes, `allowed_tools`, `enabled`, status columns for the six broken-creds surfaces.

## How it's tested

Phase 1 tests round-trip connect/callback/refresh/clear/validate against `apps/fake-linear`; Phase 1b mirrors against `apps/fake-notion`.
