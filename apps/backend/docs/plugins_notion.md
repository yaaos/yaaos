# plugins/notion

> `IntegrationProvider` for Notion — OAuth + hosted MCP wiring.

## Scope

Same shape as [`plugins/linear`](plugins_linear.md); differences are Notion-specific OAuth quirks. Implements `domain/integrations.IntegrationProvider`.

## Module architecture

`ProviderConfig`:
- OAuth URLs default to `https://api.notion.com/v1/oauth/...`.
- `mcp_url = "https://mcp.notion.com/mcp"` in prod.
- `scope_separator = " "`, `default_scopes = ()` — Notion treats scope as fixed-per-app.
- `token_auth_style = "basic"` — **Notion quirk**: token endpoint authenticates via HTTP Basic, not form-body. `core/oauth._post_token` handles both styles.
- `known_read_tools = ("search", "query_database", "retrieve_page", "retrieve_block")`.
- `known_write_tools = ("update_page", "create_comment")`.

`validate(access_token)` → `GET /v1/users/me` with `Notion-Version: 2022-06-28` header (required on every Notion API call).

## Data owned

None. `mcp_credentials` lives in [`domain/integrations`](domain_integrations.md).

## How it's tested

`apps/fake-notion` in docker-compose mirrors the Notion OAuth + MCP surface (HTTP Basic on token, `Notion-Version` header, search/page/block/comment tools). Backend integration tests use a stubbed provider; e2e drives the fake.
