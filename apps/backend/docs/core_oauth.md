# core/oauth

> Generic OAuth 2.0 authorization-code + refresh primitives.

## Purpose

Single home for the OAuth dance. Phase 1 ships `build_authorize_url`, `exchange_code`, `refresh_access_token` taking a `ProviderConfig` (URLs + client credentials + scope rules). Knows nothing about yaaos's domain — `domain/integrations` consumes it for hosted-MCP provider plugins and `plugins/github` consumes it for the GitHub OAuth login flow (collapsed in Phase 1 from the M02 `plugins/oauth_github`).

## Public interface

Planned (Phase 1):

- `build_authorize_url(config, state, scopes) -> str`
- `exchange_code(config, code, redirect_uri) -> Tokens`
- `refresh_access_token(config, refresh_token) -> Tokens`

## Module architecture

Skeleton only. No I/O outside the OAuth dance — the calling module handles persistence, signing of `state`, and audit emission.

## Data owned

None.

## How it's tested

Tests land alongside the Phase 1 implementation against the `apps/fake-linear` fake provider.
