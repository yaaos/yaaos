# domain/org_settings

> Org-scoped settings — one route per concern. Each page mounts the `OrgSettingsLayout` shell so the side-tab navigation stays consistent.

## Purpose

Per-org configuration the SPA surfaces under `/orgs/$slug/settings/*`. The pages share an `OrgSettingsLayout` shell + per-tab content. The Coding Agent detail (anchor) is the most complex of the bunch; the rest are linear settings forms.

## Public interface

Each page is mounted by `core/routing` at its respective path:

| Page | Route | File |
|---|---|---|
| Auth | `/orgs/$slug/settings/auth` | `AuthSettingsPage.tsx` |
| Members | `/orgs/$slug/settings/members` | `MembersSettingsPage.tsx` |
| Audit | `/orgs/$slug/settings/audit` | `AuditSettingsPage.tsx` |
| VCS | `/orgs/$slug/settings/vcs` | `vcs/VcsSettingsPage.tsx` |
| Coding Agents (list) | `/orgs/$slug/settings/coding-agents` | `coding_agents/CodingAgentsSettingsPage.tsx` |
| Coding Agent (detail) | `/orgs/$slug/settings/coding-agents/$pluginId` | `coding_agents/CodingAgentSettingsPage.tsx` |
| API Keys | `/orgs/$slug/settings/api-keys` | `byok/BYOKSettingsPage.tsx` |
| MCP Proxy | `/orgs/$slug/settings/mcp-proxy` | `integrations/IntegrationsSettingsPage.tsx` |

`OrgSettingsLayout` renders the left tab strip + the page body slot; each page passes `active=…` so the matching tab highlights.

### VCS page

- Empty state mounts `PluginPicker`; picking GitHub fires `useStartGithubInstall()` (POSTs `/api/github/install/start`) and navigates the browser to the returned state-signed github.com URL. Picking a non-github plugin uses `useSetVcs` directly.
- Connected state surfaces two sub-states from `/api/github/installation`: not installed on this org (button fires the same `useStartGithubInstall()` handshake), and healthy (account login + installation id + live repo list from `/api/github/repositories`). A third pseudo-state — `app_configured: false` — surfaces only when the platform yaaos GitHub App env vars are unset on the deployment; the UI shows operator guidance with no install button.
- "Manage on GitHub" links to the per-installation settings page (`installations_url` from `/api/github/installation`) — the canonical place to change which repos are accessible. There is no yaaos-side reconnect button; reinstalling and changing repo access both happen on github.com.
- "Remove" clears the org's VCS choice via `DELETE /api/vcs`; it does not uninstall the App on GitHub.
- The "Install on GitHub" path goes through a backend JSON POST rather than a direct browser nav because the auth chain reads `X-Org-Slug` + CSRF from headers, which a `window.location.href` navigation can't carry.

## Coding Agent detail — anchor

`coding_agents/CodingAgentSettingsPage.tsx` dispatches to a per-plugin component registered via `coding_agents/plugin_registry.ts`. Today `claude_code` is the only registered plugin; future coding agents register here.

`coding_agents/plugins/claude_code/ClaudeCodeSettings.tsx` is the anchor implementation:

### Composition

1. **`BrokenIntegrationsNotice`** — amber banner when the org has any MCP credential with `last_refresh_status="failed"`. Sourced from `/api/auth/me`'s `broken_integrations`.
2. **`BuilderReadOnlyBanner`** — info banner for Builder-role users. UI affordance only; the server-side `require(Action.CODING_AGENT_WRITE)` enforcement is the truth.
3. **Architecture description card** — one-paragraph static explainer.
4. **`AnthropicKeyCard`** — BYOK provider=anthropic. Write-only post-save: when a key is configured, the card shows `Configured ✓ · last set <ts>` with Test/Rotate/Clear actions; the input is hidden until Rotate is clicked. Plaintext is never read back from the backend so the UI doesn't pretend it is. Four mutations live in `coding_agents/plugins/claude_code/queries.ts`.
5. **`OrchestratorCard`** — bare `AgentEditor` for the orchestrator. Inline "overridden" badges + Reset buttons when any field differs from the plugin defaults from `/api/claude_code/defaults`.
6. **`SubAgentsCard`** — repeatable `AgentEditor` rows (1..8) with Add / Remove (last-protection). Inline duplicate-name validation.
7. **Save button** — replaces the entire settings JSONB in one PATCH; disabled when there's a duplicate sub-agent name or the count is out of range.
8. **`DangerZone`** — destructive `ConfirmModal` flow that fires `useUninstallCodingAgent`.

### Per-agent fields

`AgentEditor` exposes all four schema additions from `apps/backend/app/plugins/claude_code/settings_schema.py` (b36c824):

- `name`, `prompt`, `model`, `version`, `effort` — legacy fields.
- `use_default_system_prompt` (checkbox, default true) — when toggled off, reveals…
- `system_prompt` (textarea) — overrides the plugin's built-in system prompt for this agent.

Toggling the checkbox back to default clears any stale `system_prompt` override so the wire payload stays clean.

`mcp_proxy_ids` lives on `ClaudeCodeSettings` (not the per-agent level); the field round-trips through the form unchanged.

## Data owned

None. Each page reads through `core/api` query hooks; mutations target the existing org-settings endpoints (`/api/coding-agents`, `/api/api-keys`, `/api/mcp-proxy`, `/api/orgs`, `/api/memberships`, `/api/audit`).

## How it's tested

- `coding_agents/test/coding_agents.test.tsx` — the list page covering install / uninstall confirm.
- `coding_agents/test/plugin_registry.test.tsx` — dispatch via `getPluginSettingsComponent`.
- `byok/test/byok.test.tsx`, `integrations/test/integrations.test.tsx`, `vcs/test/vcs.test.tsx` — the per-page settings forms.
- `test/layout.test.tsx` — tab visibility per role (admin sees all six; builder sees Members only).
- The Coding Agent detail page is exercised by the PR-review e2e (which traverses the full settings → review pipeline) rather than a dedicated detail-page Vitest.
