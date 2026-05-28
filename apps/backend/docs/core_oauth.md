# core/oauth

> Generic OAuth 2.0 authorization-code + refresh primitives.

## Scope

- Owns: `build_authorize_url`, `exchange_code`, `refresh_access_token`, `ProviderConfig`, `Tokens`, `OAuthError`.
- Does NOT own: state signing (`itsdangerous`), persistence, or audit emission — callers handle those.
- Consumers: `domain/integrations` (hosted-MCP provider plugins), `plugins/github` (GitHub OAuth login).

## Why / invariants

**`token_auth_style`** — `"basic"` puts `client_id`/`client_secret` on HTTP Basic (Notion's quirk); `"form"` (default) puts them in the form body (Linear, GitHub).

**`ProviderConfig` lives here** because `exchange_code` consumes it. `domain/integrations.types` re-exports it for plugin authors who only need to declare configs.

