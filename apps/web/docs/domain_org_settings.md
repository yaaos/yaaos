# domain/org_settings

> Org-scoped settings pages under `/orgs/$slug/settings/*`, sharing `OrgSettingsLayout` for consistent tab navigation.

## Scope

Routes: `auth`, `members`, `audit`, `vcs`, `coding-agents`, `coding-agents/$pluginId`, `api-keys`, `mcp-proxy`, `workspaces` — see `core/routing` for mounts. Consumes endpoints: `/api/coding-agents`, `/api/api-keys`, `/api/mcp-proxy`, `/api/orgs`, `/api/memberships`, `/api/audit`, `/api/github/*`, `/api/vcs`, `/api/sso/*`, `/api/claude_code/*`. Owns no data.

Tab visibility is role-gated: admin sees all tabs; builder sees Members only (`test/layout.test.tsx`).

## VCS page

- **Empty state** — `PluginPicker`; GitHub selection fires `useStartGithubInstall()` (`POST /api/github/install/start`) then navigates to the returned state-signed github.com URL. Non-GitHub uses `useSetVcs` directly.
- **Connected state** — reads `/api/github/installation`; two sub-states: not-installed-on-org (re-fires install handshake) and healthy (shows account + repos from `/api/github/repositories`).
- **`app_configured: false`** — platform env vars unset; shows operator guidance, no install button.
- "Manage on GitHub" links to `installations_url` — canonical for repo access changes. No yaaos-side reconnect.
- "Remove" → `DELETE /api/vcs`; does not uninstall the GitHub App.
- Install flow uses a backend POST (not `window.location`) because `X-Org-Slug` + CSRF can't ride a bare navigation.

## Coding Agent detail

`coding_agents/CodingAgentSettingsPage.tsx` dispatches to a per-plugin component via `coding_agents/plugin_registry.ts`. `claude_code` is the only registered plugin.

`ClaudeCodeSettings.tsx` composition (top → bottom):
1. **`BrokenIntegrationsNotice`** — amber banner when any MCP credential has `last_refresh_status="failed"` (from `/api/auth/me`).
2. **`BuilderReadOnlyBanner`** — info banner; UI only. Server enforces `require(Action.CODING_AGENT_WRITE)`.
3. **`AnthropicKeyCard`** — BYOK Anthropic key. Write-only: post-save shows `Configured ✓ · last set <ts>` with Test/Rotate/Clear; plaintext never read back.
4. **`OrchestratorCard`** — `AgentEditor` for orchestrator; inline "overridden" badges + Reset when fields differ from `/api/claude_code/defaults`.
5. **`SubAgentsCard`** — 1–8 repeatable `AgentEditor` rows; inline duplicate-name validation; Add/Remove (last-row protected).
6. **Save** — one PATCH replacing the entire settings JSONB; disabled on duplicate name or out-of-range count.
7. **`DangerZone`** — `ConfirmModal` → `useUninstallCodingAgent`.

`AgentEditor` fields: `name`, `prompt`, `model`, `version`, `effort`, `use_default_system_prompt` (checkbox; default true; toggling off reveals `system_prompt` textarea). Toggling back to default clears `system_prompt` so the wire payload stays clean.

## Workspaces page

`WorkspacesSettingsPage.tsx` at `/orgs/$slug/settings/workspaces`. Admin-only. No mode selector — the system has exactly one provider (`remote_agent`). Renders:
- **AWS configuration card** — IAM role ARN input + AWS region dropdown. Save calls `PATCH /api/orgs` with `registered_iam_arn` + `aws_region`. ARN validated client-side against `arn:aws:iam::\d{12}:role/[\w+=,.@-]+`; Save disabled until valid. Server lowercases before storing and returns 422 `arn_already_registered` if another org holds the same ARN.
  - **ARN-change confirmation** — when the saved ARN differs from the current value and one or more online/stale agents exist, a `ConfirmModal` appears before saving: "This will disconnect N running WorkspaceAgents and fail their in-flight Workspaces. Continue?" N comes from `GET /api/orgs/{slug}/agents` (online + stale count). Cancel aborts the save; confirm proceeds. The modal uses the `destructive` tone.
- **Agent deployment card** — deploy snippet + backend URL + min version info.

## Tests

- `coding_agents/test/` — list page + plugin registry dispatch.
- `byok/test/`, `integrations/test/`, `vcs/test/` — per-page forms.
- `test/layout.test.tsx` — tab visibility per role.
- Detail page is covered by the PR-review e2e, not a dedicated Vitest.
