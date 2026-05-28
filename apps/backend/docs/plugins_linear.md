# plugins/linear

> `IntegrationProvider` for Linear — OAuth + hosted MCP wiring.

## Scope

Lets an org connect its Linear workspace so the reviewer agent can fetch issue context via hosted MCP. Implements `domain/integrations.IntegrationProvider`: declares `ProviderConfig` (OAuth + MCP URLs + tool catalogue) and a thin `validate(access_token)` that hits Linear's `/api/me`. No HTTP routes — proxy + OAuth callback live in [`domain/integrations`](domain_integrations.md).

## Module architecture

`ProviderConfig`:
- OAuth URLs default to `https://linear.app/oauth/...`; test stacks point at `apps/fake-linear`.
- `mcp_url = "https://mcp.linear.app/sse"` in prod.
- `scope_separator = ","`, `default_scopes = ("read",)`.
- `token_auth_style = "form"`.
- `known_read_tools = ("get_issue", "search_issues", "list_projects", "list_cycles")` — always allowed.
- `known_write_tools = ("update_issue", "create_comment")` — allowed only when org's `allowed_tools` lists them.

`validate(access_token)` → `GET /api/me`; returns True on 2xx.

## Data owned

None. `mcp_credentials` lives in [`domain/integrations`](domain_integrations.md).

## How it's tested

`apps/fake-linear` in docker-compose covers OAuth + MCP round-trips. Backend integration tests use a stubbed `IntegrationProvider`; e2e drives the fake.
