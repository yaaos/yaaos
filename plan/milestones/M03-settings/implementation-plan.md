# M03 implementation plan

> Phased build order. Read [requirements.md](requirements.md) and [architecture.md](architecture.md) first.

## Phase 0 — scaffolding

- Single named migration `0XX_create_all_m03` registered in `core/database/service.py:_MIGRATIONS` (use next available number).
- Add new columns/tables: `users.github_username`, `orgs.session_timeout_override`, `orgs.vcs_plugin_id`, `orgs.vcs_settings`, `org_coding_agents` (incl. `updated_at`), `byok_keys`.
- Create `core/secrets` module: `encrypt`/`decrypt` wrapping Fernet + master key (existing `yaaos_totp_master_key` env var with `yaaos_encryption_key` fallback). Refactor `domain/identity/totp.py` and `domain/orgs/sso.py` to import from `core/secrets` instead of their inline `_fernet()` helpers. Add `apps/backend/docs/core_secrets.md`.
- Create `core/byok` skeleton (depends on `core/secrets`) + `apps/backend/docs/core_byok.md` stub.
- No separate `domain/plugin_installs` module — VCS + coding-agent state owned by `domain/orgs`.
- Frontend: create `apps/web/src/core/sidebar`, `apps/web/src/domain/account`, `apps/web/src/domain/org_settings`, `apps/web/src/shared/plugin_picker` directories + index exports. No generic JSON-schema form module in M03.

## Phase 1 — plugin registry enumeration

- Add `list_plugins(type: PluginType) -> list[PluginMeta]` (or wherever the registry lives today; verify before writing).
- Extend `Plugin` Protocol with `settings_schema()`, `install_url(org_id)`, `validate_settings(...)`.
- Backfill the github + claude_code + in_process_workspace plugins with minimal schemas.
- Endpoint `GET /api/plugins/available?type=...` returns `[{meta, settings_schema}, ...]`.
- Tests: each existing plugin returns a non-empty meta + schema; type filter works.

## Phase 2 — domain/orgs extensions (VCS + Coding Agents) + core/byok

- `domain/orgs` gains service methods for VCS (set/get/clear) and Coding Agents (install/list/update/uninstall) per [architecture.md § Touched (significant)](architecture.md#touched-significant). All mutations emit `core/audit_log` entries.
- VCS-set: if the chosen plugin has `install_url`, return a redirect URL; otherwise store settings directly.
- Refactor M02's GitHub-App install handlers to invoke `domain/orgs.set_vcs(org_id, "github", {installation_id, ...})` after the install handshake completes.
- `core/byok` service: get/set/clear/validate per `(org_id, provider)`. `validate` takes a validator callable supplied by the provider plugin so `core/byok` stays free of provider-specific HTTP logic.
- Endpoints per [architecture.md § Routing § API](architecture.md#api).
- Tests: VCS install/uninstall round-trip; coding-agent install across two plugins; settings-validation rejection; audit entries written; `core/byok` round-trip + decrypt + validator-callable invocation.

## Phase 3 — user profile additions

- `users.github_username` column wired through `domain/identity` user service + read/write API.
- `plugins/oauth_github` login callback writes `github_username` from the GitHub `/user` response on every successful login.
- New verify-only flow in `domain/account`: `GET /api/account/github/verify` → redirects via Authlib; `GET /api/account/github/verify/callback` → exchanges code, reads username, writes `users.github_username`. No `oauth_identities` row touched.
- `PATCH /api/memberships/me/{org_id}` for handle changes (per-org).
- `PATCH /api/account/me` for display_name updates.
- Extend `GET /api/account/me` payload with `github_username` + per-org handle list.
- Tests: verify-only flow writes username without creating identity row; per-org handle update respects `UNIQUE(org_id, handle)`; login flow updates username on every login (including when value changes).

## Phase 4 — org settings additions

- `orgs.session_timeout_override` exposed via `PATCH /api/orgs/{slug}`.
- Session lookup path honors the override: when computing idle expiry for a session whose request includes a given `X-Org-Slug`, use the org's override if set, else the global constant.
- Tests: override applies, falls back when null, can be cleared by setting to null.

## Phase 5 — sidebar component

- Build `<Sidebar>` reading a static nav config typed per `architecture.md § Sidebar component model`.
- Build `<UserCard>` for the bottom slot with popover for User section.
- Collapse-state persistence in `localStorage`.
- Role-gated nav items hidden via `useCurrentUser()` membership lookup.
- Replace `AppShell`'s old nav with the new sidebar.
- Snapshot tests for collapsed + expanded states; tests for role-gated item visibility.

## Phase 6 — User section pages

- `/account/details` page: display_name editor, per-org handle table, emails read-only list, GitHub association card.
- `/account/security` page: TOTP enrollment + management (re-homed from M02 `/account`), "Sign out of all sessions" button.
- Log off action in user card popover (no page).
- Tests + E2E: edit handle in two orgs independently; connect GitHub via verify flow; toggle TOTP; sign out all sessions.

## Phase 7 — Org Settings shell + Auth + Members + Audit

- Org Settings shell at `/orgs/{slug}/settings/$section` with sub-route layout.
- Auth page: re-home M02 SSO config + add session-timeout override field.
- Members page: re-home M02 members page unchanged.
- Audit page: re-home M02 audit page unchanged.
- Update existing M02 doc pages to reflect new URLs.
- E2E: navigate through all sub-pages, verify role-gating.

## Phase 8 — Org Settings > VCS

- VCS page: empty-state picker + populated-state settings view.
- Reuses `<PluginPicker>` filtered by `PluginType.VCS`.
- Add flow: if plugin has `install_url`, redirect; otherwise settings form + save.
- Remove flow: confirmation modal, then `DELETE /api/orgs/{slug}/vcs`.
- E2E: pick github plugin → redirected to App install (use test stub from M02) → state updates to "Connected" → remove → state returns to picker.

## Phase 9 — Org Settings > Coding Agents (generic)

- Coding Agents page: list of installed + "Add coding agent" button.
- Add flow: picker filtered to plugins not yet installed for this org.
- Remove flow with confirmation.
- Per-plugin settings dispatch via the `pluginId → ReactComponent` registry. Plugins without a registered component land on a "settings not available" placeholder.
- E2E: install one plugin, remove one. (Claude Code's rich UI is exercised in Phase 10.)

## Phase 10 — Claude Code plugin bespoke UI

- `plugins/claude_code` exposes default orchestrator config + default sub-agent set as constants + a `get_defaults()` accessor. Updates `apps/backend/docs/plugins_claude_code.md` to describe the orchestrator/sub-agents model.
- Settings model in `domain/plugin_installs` for `claude_code`: Pydantic model enforcing sub-agent name uniqueness, sub-agent count ≥ 1 and ≤ 8, model/version/effort never blank, name length ≤ 64.
- Backend endpoint `GET /api/orgs/{slug}/coding-agents/claude_code/defaults` returns the code defaults.
- Frontend `domain/org_settings/claude_code` page:
  - One-paragraph architecture description (static copy).
  - Anthropic API key field (reveal/hide, test, save) reading and writing `byok_keys` for provider=anthropic.
  - Orchestrator section: collapsible prompt textarea, model/version/effort dropdowns, per-field reset/overridden indicators, `updated_at` display.
  - Sub-agents section: list with collapse-per-agent, add button (disabled at 8), remove button (disabled on last), inline name uniqueness validation, same prompt/model UI as orchestrator. Reset/overridden only for code-seeded sub-agents.
  - Defaults fetched from the dedicated endpoint and held in client state for the page lifetime.
- One audit entry per save action (action `coding_agent.claude_code.settings_saved`, metadata lists changed top-level sections).
- E2E: install Claude Code in a fresh org → defaults populate UI → edit orchestrator prompt → reset it → add a sub-agent → rename to duplicate-of-existing → assert validation error → remove a sub-agent down to 1 → assert further remove blocked.

## Phase 11 — Org Settings > BYOK UI

- `core/byok` already exists from Phase 2. Phase 11 wires the UI.
- Anthropic validate-call lives in `plugins/claude_code` (or a dedicated Anthropic plugin module): a minimal `messages.create` request with 1 output token. Passed to `core/byok.validate` as the validator callable.
- Endpoints per [architecture.md § API](architecture.md#api).
- Frontend `domain/org_settings/byok` page: provider list (Anthropic only) with status, reveal/save/test/clear actions.
- Same record surfaced in Claude Code settings page; both write paths go through `core/byok`.
- Audit log: `byok.set`, `byok.cleared`, `byok.validated`.
- E2E: set key → test → save → confirm Claude Code page reflects the change → clear from BYOK → confirm Claude Code page shows empty state.

## Phase 12 — docs + cleanup + final verification

- Per-module docs filled: `core_byok.md`. Updates to `domain_orgs.md` (VCS + coding-agents methods + session-timeout override), `domain_identity.md` (`github_username` field + verify-only flow), `plugins_oauth_github.md` (updates `github_username` on login), `plugins_claude_code.md` (orchestrator/sub-agent model + defaults endpoint + BYOK consumer + Anthropic validator).
- `docs/system-architecture.md` updated for the new settings surface + plugin-install flow.
- `apps/backend/docs/patterns.md` and `apps/web/docs/patterns.md` updated for the sidebar / plugin-picker patterns.
- `docs/glossary.md` adds: VCS plugin, coding agent, plugin install, verified GitHub username, session-timeout override.
- `apps/backend/bin/sync_modules`; full CI green; security scan clean.

## Dependency order

```
0 → 1 → 2 ┬─→ 3 → 4 → 5 → 6 ┐
          └────────────────→ 7 → 8 → 9 → 10 → 11 → 12
```

Phases 1+2 unblock both the user-section (3+) and org-settings (7+) tracks. Sidebar (5) can be built in parallel with backend phases once data shape is locked. Phase 10 (Claude Code rich UI) depends on Phase 9 (Coding Agents shell) and Phase 11 (BYOK) is independent of 10 but easiest to ship after — the Claude Code page reads BYOK.

## Risks

- **Plugin registry refactor scope** — extending the Plugin Protocol touches every existing plugin. Keep additions minimal.
- **VCS install flow rewiring** — M02's signed-state GitHub App install handshake needs to route through `domain/plugin_installs.set_vcs` rather than directly. Watch for missed call sites; grep for `github_installations` insert sites.
- **Verify-only callback URL on the OAuth App** — confirm GitHub OAuth Apps allow multiple callback URLs on the same host. (They do, but worth verifying before assuming.)
- **Sidebar collapse + active-route highlight** — small UI bug surface; snapshot tests + manual click-through cover it.
- **Claude Code defaults endpoint cache risk** — make sure the endpoint imports defaults at request time, not at module load, so a code change to defaults is reflected on next request.
- **BYOK key encryption** — reuse the M02 master key. Don't introduce a second secret-management story for M03.
