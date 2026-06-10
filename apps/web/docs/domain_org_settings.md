# domain/org_settings

> Org-scoped settings pages under `/orgs/$slug/settings/*`, sharing `OrgSettingsLayout` for consistent tab navigation.

## Scope

Routes: `auth`, `members`, `audit`, `vcs`, `coding-agents`, `coding-agents/$pluginId`, `api-keys`, `mcp-proxy`, `workspaces` — see `core/routing` for mounts. Consumes endpoints: `/api/coding-agents`, `/api/api-keys`, `/api/mcp-proxy`, `/api/orgs`, `/api/memberships`, `/api/audit`, `/api/github/*`, `/api/vcs`, `/api/sso/*`, `/api/claude_code/*`. Owns no data.

Tab visibility is role-gated: admin sees all tabs; builder sees Members only (`test/layout.test.tsx`).

## VCS page

- **Loading** — `useVcsState` uses `useSuspenseQuery`; page body renders under `<ErrorBoundary>` + `<Suspense>`.
- **Empty state** — "Connect GitHub" card with a single CTA; clicking it fires `useStartGithubInstall()` (`POST /api/github/install/start`) then navigates to the returned state-signed github.com URL.
- **Connected state** — reads `/api/github/installation` (`useSuspenseQuery`); two sub-states: not-installed-on-org (re-fires install handshake) and healthy (shows account + repos from `/api/github/repositories` (`useSuspenseQuery`)).
- **`app_configured: false`** — platform env vars unset; shows operator guidance, no install button.
- "Manage on GitHub" links to `installations_url` — canonical for repo access changes. No yaaos-side reconnect.
- "Remove" → `DELETE /api/vcs`; does not uninstall the GitHub App.
- Install flow uses a backend POST (not `window.location`) because `X-Yaaos-Org-Slug` + CSRF can't ride a bare navigation.

## Coding Agents list

`CodingAgentsSettingsPage` renders under `<ErrorBoundary>` + `<Suspense>`; data from `useCodingAgents` (`useSuspenseQuery`). "Add coding agent" opens an install card with a direct "Add Claude Code" button — `claude_code` is the only available plugin and is disabled when already installed.

## Coding Agent detail

`coding_agents/CodingAgentSettingsPage.tsx` dispatches to a per-plugin component via `coding_agents/plugin_registry.ts`. `claude_code` is the only registered plugin.

`ClaudeCodeSettings` renders under `<ErrorBoundary>` + `<Suspense>`; data from `useCodingAgents` (`useSuspenseQuery`).

`ClaudeCodeSettings.tsx` composition (top → bottom):
1. **`BrokenIntegrationsNotice`** — amber banner when any MCP credential has `last_refresh_status="failed"` (from `/api/auth/me`).
2. **`BuilderReadOnlyBanner`** — info banner; UI only. Server enforces `require(Action.CODING_AGENT_WRITE)`.
3. **`AnthropicKeyCard`** — BYOK Anthropic key. Write-only: post-save shows `Configured ✓ · last set <ts>` with Test/Rotate/Clear; plaintext never read back.
4. **`RepoSkillsCard`** — per-repo skill name text inputs. Calls `GET /api/claude_code/repos` (`useClaudeCodeRepos`) for the live repo list joined with stored skill names. Each row (`RepoSkillRow`) has an uncontrolled text input and a Save button that fires `PUT /api/claude_code/repos/{encodeURIComponent(owner/repo)}` (`useSetRepoSkill`). Empty state shown when no repos are connected. Renders under its own `<ErrorBoundary>` + `<Suspense>`.
5. **`DangerZone`** — `ConfirmModal` → `useUninstallCodingAgent`.

## Forms

All input forms across the settings pages use `react-hook-form` + Zod (`zodResolver`). shadcn `form.tsx` primitives (`Form`, `FormField`, `FormItem`, `FormControl`, `FormMessage`) carry validation messages automatically. Affected pages: `AuthSettingsPage` (session-timeout), `WorkspacesSettingsPage` (ARN + region), `BYOKSettingsPage` (API key per provider card), `ClaudeCodeSettings` (Anthropic key card + agent-config Save), `IntegrationsSettingsPage` (allowlist add). Simple action buttons (toggle enabled, disconnect) are not wrapped in RHF forms.

## Workspaces page

`WorkspacesSettingsPage.tsx` at `/orgs/$slug/settings/workspaces`. Admin-only. No mode selector — the system has exactly one provider (`remote_agent`). Renders under `<ErrorBoundary>` + `<Suspense>` (both `useOrgSettings` and `useAgents` are awaited before content renders). Renders:
- **AWS configuration card** — IAM role ARN input + AWS region dropdown (RHF + Zod; ARN validated against `arn:aws:iam::\d{12}:role/[\w+=,.@-]+`). Save calls `PATCH /api/orgs` with `registered_iam_arn` + `aws_region`. Server lowercases before storing and returns 422 `arn_already_registered` if another org holds the same ARN.
  - **ARN-change confirmation** — when the saved ARN differs from the current value and one or more online/stale agents exist, a `ConfirmModal` appears before saving: "This will disconnect N running WorkspaceAgents and fail their in-flight Workspaces. Continue?" N comes from `GET /api/orgs/{slug}/agents` (online + stale count). Cancel aborts the save; confirm proceeds. The modal uses the `destructive` tone.
- **Agent deployment card** — deploy snippet + backend URL + min version info.

## Public interface

Router imports each page directly by path; no barrel.

- `public/AuditSettingsPage.tsx` — `AuditSettingsPage`
- `public/AuthSettingsPage.tsx` — `AuthSettingsPage`
- `public/MembersSettingsPage.tsx` — `MembersSettingsPage`
- `public/WorkspacesSettingsPage.tsx` — `WorkspacesSettingsPage`
- `public/byok/BYOKSettingsPage.tsx` — `BYOKSettingsPage`
- `public/coding_agents/CodingAgentSettingsPage.tsx` — `CodingAgentSettingsPage` (per-plugin dispatch)
- `public/coding_agents/CodingAgentsSettingsPage.tsx` — `CodingAgentsSettingsPage` (list)
- `public/integrations/IntegrationsSettingsPage.tsx` — `IntegrationsSettingsPage`
- `public/vcs/VcsSettingsPage.tsx` — `VcsSettingsPage`

Private (not in `public/`): `OrgSettingsLayout`, `queries.ts` (root + each sub-folder), `AuditPage.tsx`, `MembersPage.tsx`, `SsoConfigPage.tsx`, `coding_agents/plugin_registry.ts`, `coding_agents/plugins/**`.

`CodingAgentSettingsPage` carries the `import "../../coding_agents/plugins/claude_code"` side-effect that registers the plugin before the first `getPluginSettingsComponent` call.

## Tests

All settings tests use MSW to intercept HTTP rather than `vi.mock("../queries")`.

- `coding_agents/test/coding_agents.test.tsx` — component/MSW: empty state, Add card with claude_code disabled when already installed, install flow, Remove confirmation, settings link.
- `coding_agents/test/plugin_registry.test.tsx` — unit: dispatch to registered vs. unknown plugin.
- `coding_agents/plugins/claude_code/test/claude_code_settings.test.tsx` — component/MSW: renders one input per repo, Save fires PUT with `encodeURIComponent`-encoded path (regression guard for the `%2F`-before-routing bug), empty state when no repos connected.
- `byok/test/byok.test.tsx` — component/MSW: not_set / configured / rotate states; save / test / clear flows.
- `integrations/test/integrations.test.tsx` — component/MSW: connect flow, allowlist, enabled toggle, disconnect.
- `vcs/test/vcs.test.tsx` — component/MSW: Connect GitHub card, connected, needs-setup, unprovisioned states; remove confirmation.
- `test/layout.test.tsx` — tab visibility per role.
