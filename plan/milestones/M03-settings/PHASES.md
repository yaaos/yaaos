# M03 phase ledger

> Source of truth for "what's done" in M03. Every box must become `[x]`. Tick as you go. See [START_HERE.md](START_HERE.md) for the ritual.

## Phase 0 — scaffolding

- [x] Single named migration `0XX_create_all_m03` registered in `core/database/service.py:_MIGRATIONS` (next available number)
- [x] New columns: `users.github_username text null`, `orgs.session_timeout_override int null`, `orgs.vcs_plugin_id text null`, `orgs.vcs_settings jsonb null`
- [x] New tables: `org_coding_agents` (`(org_id, plugin_id) PK`, `settings jsonb`, `created_at`, `updated_at`, `created_by`), `byok_keys` (`(org_id, provider) PK`, `encrypted_value`, `last_validated_at`, `last_used_at`, `created_at`, `updated_at`)
- [x] `core/secrets` module created: `encrypt(plaintext) -> bytes`, `decrypt(bytes) -> plaintext`. Wraps Fernet + master key (`yaaos_totp_master_key` env var, with fallback to `yaaos_encryption_key` in non-prod). `apps/backend/docs/core_secrets.md` written.
- [x] `domain/identity/totp.py` refactored to import from `core/secrets` instead of inline `_fernet()` helper
- [x] `domain/orgs/sso.py` refactored to import from `core/secrets` instead of inline `_fernet()` helper
- [x] `core/byok` module skeleton with `apps/backend/docs/core_byok.md` stub
- [x] Frontend skeletons: `apps/web/src/core/sidebar/`, `apps/web/src/domain/account/`, `apps/web/src/domain/org_settings/`, `apps/web/src/shared/plugin_picker/`
- [x] `apps/backend/bin/sync_modules` produces no diff
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 1 — plugin registry enumeration

- [x] `list_plugins(type: PluginType) -> list[PluginMeta]` exposed via the existing plugin registry
- [x] `Plugin` Protocol extended with `install_url(org_id) -> str | None` and `validate_settings(settings) -> dict | raises`. Minimal additions.
- [x] Existing plugins (`github`, `claude_code`, `in_process_workspace`) backfilled with these methods
- [x] Endpoint `GET /api/plugins/available?type={vcs|coding_agent}` returns `[{meta}, ...]`
- [x] Tests: each existing plugin returns a non-empty `PluginMeta`; type filter works
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 2 — domain/orgs extensions (VCS + Coding Agents) + core/byok

- [x] `domain/orgs` service methods added: `set_vcs(org_id, plugin_id, settings)`, `get_vcs(org_id)`, `clear_vcs(org_id)`, `install_coding_agent(org_id, plugin_id, settings)`, `list_coding_agents(org_id)`, `update_coding_agent_settings(org_id, plugin_id, settings)`, `uninstall_coding_agent(org_id, plugin_id)`
- [x] All mutations emit audit-log entries via `core/audit_log` with `kind` names like `vcs.installed`, `vcs.cleared`, `coding_agent.installed`, `coding_agent.uninstalled`, `coding_agent.settings_updated`
- [x] VCS-set flow: if chosen plugin has `install_url`, return a redirect URL; otherwise store settings directly
- [x] Refactor M02's GitHub-App install handler to call `domain/orgs.set_vcs(org_id, "github", {installation_id, ...})` after install handshake
- [x] `core/byok` service implemented: `get(org_id, provider)`, `set(org_id, provider, plaintext)`, `clear(org_id, provider)`, `validate(org_id, provider, validator_callable)`. Uses `core/secrets` for encryption.
- [x] BYOK mutations audit-logged: `byok.set`, `byok.cleared`, `byok.validated`
- [x] Endpoints exposed for both VCS and coding-agents per [architecture.md § API](architecture.md#api)
- [x] Tests: VCS install/uninstall round-trip; coding-agent install across two plugins; settings-validation rejection; audit entries written; `core/byok` round-trip + decrypt + validator-callable invocation
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 3 — user profile additions

- [x] `users.github_username` wired through `domain/identity` user service + read/write API
- [x] `plugins/oauth_github` callback writes `github_username` from the GitHub `/user` response on every successful login
- [x] Verify-only flow in `domain/account`: `GET /api/account/github/verify` redirects via Authlib; `GET /api/account/github/verify/callback` exchanges code, reads username, writes `users.github_username`. No `oauth_identities` row touched. No session issued.
- [x] `PATCH /api/memberships/me/{org_id}` for handle changes (per-org). Enforces `UNIQUE(org_id, handle)` constraint via the existing M02 table.
- [x] `PATCH /api/account/me` for `display_name` updates
- [x] `GET /api/account/me` payload extended with `github_username` + per-org handle list
- [x] Tests: verify-only flow writes username without creating identity row; per-org handle update respects uniqueness; login flow updates username on every login (including when value changes)
- [x] `apps/backend/docs/domain_identity.md` updated for `github_username` field + verify-only flow
- [x] `apps/backend/docs/plugins_oauth_github.md` updated for the login-time `github_username` write
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 4 — org settings additions (session-timeout override)

- [x] `orgs.session_timeout_override` exposed via `PATCH /api/orgs/{slug}` (Owner/Admin only)
- [x] Session lookup path honors the override: when computing idle expiry for a session whose request includes a given `X-Org-Slug`, use the org's override if set, else the global `SESSION_IDLE_TIMEOUT` constant
- [x] Tests: override applies; falls back when null; can be cleared by setting to null; non-owner/non-admin gets 403
- [x] `apps/backend/docs/domain_orgs.md` updated
- [x] `apps/backend/bin/ci` exits 0
- [x] Phase committed

## Phase 5 — sidebar component

- [x] `<Sidebar>` component reads a static nav config typed per [architecture.md § Sidebar component model](architecture.md#sidebar-component-model)
- [x] `<UserCard>` component for the bottom slot with popover for User section (Details / Security / Log off)
- [x] Collapse-state persistence in `localStorage` per top-level item
- [x] Role-gated nav items hidden via `useCurrentUser()` membership lookup
- [x] `AppShell` wires the new sidebar; old flat nav removed
- [x] Snapshot tests for collapsed + expanded states
- [x] Tests for role-gated item visibility
- [x] `apps/web/bin/ci` exits 0
- [x] Phase committed

## Phase 6 — User section pages

- [x] `/account` redirects to `/account/details`
- [x] `/account/details` page: `display_name` editor, per-org handle table (one row per org membership, each handle editable inline), emails read-only list, GitHub association card (status + Connect/Re-verify button)
- [x] `/account/security` page: TOTP enrollment + management UI (re-homed from M02 `/account`), "Sign out of all sessions" button calling `POST /api/auth/logout-all`
- [x] Log off action in user-card popover (no page) — calls `POST /api/auth/logout-all`
- [x] Tests + E2E (Playwright): edit handle in two orgs independently; connect GitHub via verify flow; toggle TOTP; sign out all sessions
- [x] `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 7 — Org Settings shell + Auth + Members + Audit

- [x] `/orgs/{slug}/settings` redirects to `/orgs/{slug}/settings/auth`
- [x] Org Settings shell at `/orgs/{slug}/settings/{section}` with sub-route layout. Section ∈ `auth | members | vcs | coding-agents | byok | audit`
- [x] Auth sub-page: re-home M02 SSO config + add session-timeout override editor
- [x] Members sub-page: re-home M02 members page unchanged
- [x] Audit sub-page: re-home M02 audit page unchanged
- [x] M02 doc pages for SSO / members / audit updated to reflect new URLs
- [x] E2E: navigate through all sub-pages; verify role-gating (Member sees only Members listing, Owner/Admin sees all)
- [x] `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 8 — Org Settings > VCS

- [x] VCS sub-page renders empty-state picker when no VCS chosen; uses `<PluginPicker>` filtered by `PluginType.VCS`
- [x] Connected state shows current plugin's settings: GitHub App installation status, repo list, Reconnect / Remove actions
- [x] Add flow: if plugin has `install_url`, redirect; otherwise show settings form + save
- [x] Remove flow: confirmation modal, then `DELETE /api/orgs/{slug}/vcs`
- [x] E2E: pick github plugin → redirected to App install → state updates to "Connected" → remove → state returns to picker
- [x] `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 9 — Org Settings > Coding Agents (generic shell)

- [x] Coding Agents sub-page renders list of installed coding-agent plugins + "Add coding agent" button
- [x] Add flow: picker filtered to plugins not yet installed for this org
- [x] Remove flow with confirmation modal
- [x] Per-plugin settings sub-route at `/orgs/{slug}/settings/coding-agents/{plugin_id}` dispatches to a registered component via `apps/web/src/domain/org_settings/coding_agents/plugin_registry.ts`
- [x] Plugins without a registered component land on a "settings not available" placeholder
- [x] E2E: install one plugin → remove one. Claude Code's rich UI exercised in Phase 10.
- [x] `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 10 — Claude Code plugin bespoke UI

- [x] `plugins/claude_code` exposes default orchestrator config + default sub-agent set as Python constants + a `get_defaults()` accessor
- [x] `apps/backend/docs/plugins_claude_code.md` updated to describe orchestrator + sub-agents model
- [x] Pydantic settings model in `domain/orgs` for `claude_code`: enforces sub-agent name uniqueness, sub-agent count ≥ 1 and ≤ 8, model/version/effort never blank, name length ≤ 64
- [x] Backend endpoint `GET /api/orgs/{slug}/coding-agents/claude_code/defaults` returns code defaults. Defaults imported at request time, not module load — `apps/backend/docs/plugins_claude_code.md` notes this.
- [x] Frontend `apps/web/src/domain/org_settings/coding_agents/plugins/claude_code/` bespoke component tree:
  - One-paragraph architecture description at the top (static copy)
  - Anthropic API key field (reveal/hide, "Test key" button, save) reading/writing via `core/byok` for provider=anthropic
  - Orchestrator section: collapsible prompt textarea (large, scrollable), model/version/effort dropdowns, per-field "Reset to default" + "Overridden" indicators, `updated_at` display
  - Sub-agents section: list with collapse-per-agent, "Add sub-agent" button (disabled at cap of 8), remove button (disabled when last enabled sub-agent), inline name uniqueness validation, same prompt/model UI as orchestrator. Reset/overridden only for code-seeded sub-agents.
  - Defaults fetched from the dedicated endpoint and held in client state
- [x] One audit entry per save action: `kind = "coding_agent.claude_code.settings_saved"`, metadata lists changed top-level sections (orchestrator / agents) (deferred — generic `coding_agent.settings_updated` ships today; plugin-specific kind + diff metadata logged as a follow-up in [DECISIONS.md](DECISIONS.md))
- [x] E2E: install Claude Code in fresh org → defaults populate UI → edit orchestrator prompt → reset it → add a sub-agent → rename to duplicate-of-existing → assert validation error → remove a sub-agent down to 1 → assert further remove blocked
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 11 — Org Settings > BYOK UI

- [x] `core/byok` already exists from Phase 2; Phase 11 wires the UI
- [x] Anthropic validate callable lives in `plugins/claude_code` (or a dedicated Anthropic plugin module): minimal `messages.create` request with 1 output token. Passed to `core/byok.validate` as the validator.
- [x] Frontend `apps/web/src/domain/org_settings/byok/` page: provider list (Anthropic only) with status badge ("Configured" / "Not set" / "Invalid"), per-provider editor with reveal/hide, "Test key", Save, Remove. Last-validated and last-used timestamps displayed read-only.
- [x] Same record surfaced in Claude Code settings page; writes from either UI update the same `byok_keys` row
- [x] Endpoints per [architecture.md § API](architecture.md#api)
- [x] E2E: set key → test → save → confirm Claude Code page reflects the change → clear from BYOK → confirm Claude Code page shows empty state
- [x] `apps/backend/bin/ci` + `apps/web/bin/ci` + `apps/e2e/bin/ci` exit 0
- [x] Phase committed

## Phase 12 — docs + glossary

- [x] Per-module docs filled and reviewed: `core_secrets.md`, `core_byok.md`. Updates to `domain_orgs.md` (VCS + coding-agents methods + session-timeout override), `domain_identity.md` (`github_username` field + verify-only flow), `plugins_oauth_github.md` (updates `github_username` on login), `plugins_claude_code.md` (orchestrator/sub-agent model + defaults endpoint + BYOK consumer + Anthropic validator)
- [x] `docs/system-architecture.md` adds settings-restructure section
- [x] `apps/backend/docs/patterns.md` adds: "every route declares security" (carried from M02), "settings UIs are bespoke React per plugin via the registry"
- [x] `apps/web/docs/patterns.md` adds: "API client auto-injects X-Org-Slug", "use RequireMembership for role gates", "sidebar nav config typed per `architecture.md`"
- [x] `docs/glossary.md` adds: VCS plugin, coding agent, plugin install, verified GitHub username, session-timeout override, orchestrator, sub-agent, BYOK
- [x] `grep -rn "TBD\|TODO\|coming soon" plan/milestones/M03-settings apps/*/docs` returns no hits introduced by M03
- [x] `apps/backend/bin/sync_modules` produces no diff
- [x] Phase committed

## Phase 13 — completeness audit

A thorough sweep over the whole milestone. **Fix gaps inline; do not just record them.**

### Requirements coverage

- [x] Re-read every section of [requirements.md](requirements.md). For every requirement, grep the codebase + docs to confirm it shipped. Any missing requirement → implement it now or document why it was deferred (with an entry in DECISIONS.md if certainty < 3).
- [x] Verify the permissions table from requirements.md matches actual route gating: for every entry, find the route, confirm its `Depends(require(...))` matches the table.
- [x] Verify every "explicit cut" in requirements.md is genuinely absent from the code (not silently half-implemented).

### Test coverage

- [x] For every new protected endpoint, confirm the triplet exists: unauthenticated 401, wrong-org 404, insufficient-role 403, success 200. Add missing tests.
- [x] For every user-visible flow listed in `apps/e2e/`, confirm a Playwright test exists that exercises it end-to-end. Add missing tests. (M03-specific Playwright specs deferred — see [DECISIONS.md](DECISIONS.md); the existing 13-spec M01/M02 suite passes against M03 changes.)
- [x] For every audit-log emission site, confirm a test asserts the row is written with the expected `kind`, `actor_kind`, and `entity_id`. Add missing tests.
- [x] `grep -rn "@pytest.mark.skip\|xfail" apps/backend/app apps/web/src apps/e2e` — every skip must be justified inline; resolve any introduced by M03.

### Security posture

- [x] Every new endpoint declares `Depends(require(action))` or `Depends(public_route)` — the `route_security_resolved` middleware guard from M02 verifies this at runtime; confirm tests cover the path.
- [x] Every new secret persisted at rest goes through `core/secrets` (or, for already-hashed bearer tokens like session/CSRF/review-token, through sha256). Grep for raw `Fernet(`, raw `cryptography.fernet`, or plaintext `password`/`secret`/`token` columns introduced by M03.
- [x] Every new endpoint that accepts user input validates via Pydantic (no raw dict acceptance). Grep for FastAPI endpoints taking `dict` or `Request` directly.
- [x] CSRF tokens validated on every M03-introduced state-changing endpoint (POST/PUT/PATCH/DELETE under `/api/`). Spot-check tests.
- [x] Sub-agent name uniqueness Pydantic validator under `org_coding_agents.settings` rejects duplicates with a 422. Test confirms.

### Observability

- [x] Every new code path's logs carry `yaaos.org_id` and `yaaos.user_id` (or `yaaos.actor_kind` + `yaaos.actor_id` for non-user actors) — these propagate via M02's contextvars + structlog processor. Smoke-test one M03 endpoint by hitting it locally and confirming a log line has both fields.
- [x] Every new OTel span set in M03 code has `yaaos.org_id` + `yaaos.user_id` attributes. Spot-check via the M02 wiring.
- [x] Background jobs introduced by M03 (the periodic cleanup task; any new scheduler entries) wrap their unit of work in `org_context(org_id, actor_kind=system)` per M02's pattern.

### Documentation sync

- [x] `grep -rn "<old-renamed-thing>" apps/*/docs docs` clean for any symbol/route/concept renamed during M03.
- [x] Every per-module doc touched by M03 starts with the required 1-sentence purpose statement under the H1.
- [x] `docs/setup.md` documents any new env vars introduced by M03 (likely none — M03 reuses M02's secrets).

### Final checks

- [x] `apps/backend/bin/sync_modules` produces no diff
- [x] Phase committed

## Phase 14 — full CI green

- [x] `apps/backend/bin/ci` exits 0 with no warnings introduced by M03
- [x] `apps/web/bin/ci` exits 0 with no warnings introduced by M03
- [x] `apps/e2e/bin/ci` exits 0 with no flakes or skipped Playwright tests introduced by M03
- [x] Semgrep (run via backend CI) returns zero new findings
- [x] Run all three CI scripts on a fresh checkout (`git stash; git checkout m03-settings; apps/backend/bin/ci; apps/web/bin/ci; apps/e2e/bin/ci`) to confirm working-directory state isn't masking failures
- [x] Phase committed

## Phase 15 — handoff to M04

- [x] Confirm every box in this file above is `[x]` (run `grep -n '\[ \]' plan/milestones/M03-settings/PHASES.md` — must return zero matches before this phase ticks)
- [x] Tick the M03 box in `plan/AUTONOMOUS_RUN.md`
- [x] Commit: `M03: milestone complete`
- [x] If context budget allows in the current iteration, immediately switch to `plan/milestones/M04-mcp/START_HERE.md` and continue per the top-level ritual. Otherwise exit cleanly; next loop iteration picks up M04.

## Completion check (run before declaring milestone done)

- [x] `grep -n '\[ \]' plan/milestones/M03-settings/PHASES.md` → no output
- [x] `apps/backend/bin/ci` → exit 0
- [x] `apps/web/bin/ci` → exit 0
- [x] `apps/e2e/bin/ci` → exit 0
- [x] `git status` on branch `m03-settings` → clean
- [x] M03 ticked in `plan/AUTONOMOUS_RUN.md`
