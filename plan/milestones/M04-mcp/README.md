# M04 — MCP context for reviewer agents

> Lets reviewer agents reach a company's external tools (starting with Linear) by speaking MCP through a yaaos-owned proxy. Adds outbound OAuth, per-review tokens, audit per JSON-RPC method, and Settings UI to manage org-enabled providers + per-user connections.

## Status

`[planned]` — sequenced **after M03**. Designed to run back-to-back with M03 in an autonomous loop. Reuses M02 modules (audit, sessions, encryption master key) and M03 modules (sidebar, org-settings shell, BYOK encryption pattern).

## Reading order

1. [requirements.md](requirements.md) — locked spec: what MCP does for yaaos, what we ship in M04, what we cut.
2. [architecture.md](architecture.md) — `domain/integrations` (outbound OAuth) + `domain/mcp_proxy` (the proxy), data model, refresh serialization, proxy lifecycle.
3. [implementation-plan.md](implementation-plan.md) — phased build order.

## Scope at a glance

- **Two providers**: Linear and Notion. Sentry / Slack / GitHub MCP deferred.
- **Pattern B (proxy-mediated MCP).** yaaos runs its own MCP server. The workspace's `.mcp.json` points the Claude Code CLI at yaaos. yaaos forwards JSON-RPC to the upstream hosted MCP using the org's service-account OAuth bearer. Workspace never sees the upstream token.
- **One credential per (org, provider).** Single org service-account model: Owner connects yaaos to Linear / Notion once for the org; every review uses those creds. No per-user credentials, no fallback tiers. Standard pattern for team-scoped integrations.
- **Outbound OAuth.** yaaos-as-OAuth-client for Linear + Notion. New `domain/integrations` module mirrors `plugins/oauth_github`'s shape but persists to `mcp_credentials`.
- **Per-review token** mints a yaaos-only bearer that's the workspace's only outbound capability for that review. Time-bound, revoked at review teardown.
- **Audit one row per JSON-RPC method** (initialize / tools/list / tools/call / etc.). `actor_kind` reflects who triggered the review (user-triggered → `user`; webhook-triggered → `system`); `payload.upstream_account = "org_service_account"` records which creds were used upstream. Args hashed, not stored raw.
- **Settings UI**:
  - Org Settings > Integrations — Owner/Admin only: enable/disable per provider, connect/reconnect the org service-account, configure the org allowlist (read default on, write opt-in).
  - No User > Connections page; per-user credentials are out of scope.

## Out of scope (deferred)

- Per-user credentials. Single org service account only.
- Providers beyond Linear + Notion (Sentry, Slack, GitHub MCP).
- REST-shim path (yaaos implementing the MCP server itself for providers without hosted MCP).
- Per-repo overrides.
- "Routed" context (PR-mention-driven server enable).
- Containerized workspace egress firewall (depends on a separate workspace-isolation milestone).
- Per-tool fine-grained allowlist UI (M04 has org-level read/write toggles per provider).

## Decisions locked (resolving open questions from `plan/notes/mcp-context.md`)

- **Q1 webhook fallback**: not needed. Single org service-account is the only credential. `mcp_credentials` keyed by `(org_id, provider)`. Every review uses the org account regardless of trigger.
- **Q2 OAuth vs PAT**: OAuth from the start.
- **Q3 Hosted vs REST-shim**: Hosted forward-path only.
- **Q4 unconnected tool behavior**: Proxy returns a structured `not_connected` MCP error. Agent prompts include guidance to acknowledge the missing context and continue.
- **Q5 read vs write tools**: Read-only by default. Write tools opt-in via the org's allowlist (`mcp_credentials.allowed_tools text[]`).

## Source

Matured from [plan/notes/mcp-context.md](../../notes/mcp-context.md). Note kept until M04 ships, then deleted (Phase last).
