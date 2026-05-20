# M03 architecture

> Module layout, plugin contracts, routing. Read [requirements.md](requirements.md) first.

## Frontend modules

### New

- `apps/web/src/core/sidebar` — sidebar shell component, collapse-state hook, user card.
- `apps/web/src/domain/account` — User section pages (`details`, `security`). Replaces M02's single `/account` page.
- `apps/web/src/domain/org_settings` — Org Settings shell + sub-pages (`auth`, `members`, `vcs`, `coding_agents`, `byok`, `audit`). Absorbs M02's standalone members / SSO / audit pages.
- `apps/web/src/shared/plugin_picker` — picker component used by VCS and Coding Agents sections; takes a list of `PluginMeta` + click handler.

### Per-plugin coding-agent settings (nested under Coding Agents)

Coding Agents sub-page hosts the per-plugin settings under one URL pattern: `/orgs/{slug}/settings/coding-agents/{plugin_id}`. The page reads `plugin_id` from the route param and dispatches to a registered React component via `apps/web/src/domain/org_settings/coding_agents/plugin_registry.ts` (a `{plugin_id → Component}` map).

- `apps/web/src/domain/org_settings/coding_agents/plugins/claude_code/` — bespoke component tree for the Claude Code settings page. Orchestrator + sub-agents UI, API-key field, reset-to-default + overridden-indicator. Hard-coupled to the backend's `claude_code` settings shape — acceptable because plugins are first-party (monorepo).
- Plugins without a registered component fall back to a "settings not available" placeholder. No generic JSON-schema renderer in M03.

### BYOK section

- `apps/web/src/domain/org_settings/byok` — BYOK section. List of providers (M03: Anthropic only), per-provider editor with reveal/test/save/remove. Backend is `core/byok` (see below).

### Touched

- `apps/web/src/core/routing/router.tsx` — adds `/account/details`, `/account/security`, `/orgs/$slug/settings/$section`. Re-homes the M02 routes accordingly.
- `apps/web/src/core/layout` (`AppShell`) — wires the new sidebar component.
- Every page that referenced the old flat sidebar items.

## Backend modules

### New

- `core/secrets` — thin Fernet wrapper around the M02 master key (`yaaos_totp_master_key` with dev fallback to `yaaos_encryption_key`). Exposes `encrypt(plaintext) -> bytes` and `decrypt(bytes) -> plaintext`. Pure infra; no domain awareness. The existing inline `_fernet()` helpers in `domain/identity/totp.py` and `domain/orgs/sso.py` (M02) are refactored to import from `core/secrets` instead of duplicating the pattern. Future encrypted-at-rest consumers (BYOK, M04 integrations) depend on this single module.
- `core/byok` — encrypted credential storage per `(org_id, provider)`. Domain-aware (knows about `org_id`) but no biz logic. Service exposes: `get(org_id, provider)`, `set(org_id, provider, plaintext)`, `clear(org_id, provider)`, `validate(org_id, provider, validator)` (validator callable supplied by the consuming provider plugin — BYOK does decrypt + call, not the upstream HTTP). Encryption goes through `core/secrets`. Audit-logged.

### Touched (significant)

- `domain/orgs` — absorbs all per-org configuration that M03 introduces. New service methods:
  - VCS: `set_vcs(org_id, plugin_id, settings)`, `get_vcs(org_id)`, `clear_vcs(org_id)`.
  - Coding agents: `install_coding_agent(org_id, plugin_id, settings)`, `list_coding_agents(org_id)`, `update_coding_agent_settings(org_id, plugin_id, settings)`, `uninstall_coding_agent(org_id, plugin_id)`.
  - Session timeout: existing org-update endpoint extended with `session_timeout_override`.
  All mutations emit audit-log entries via `core/audit_log`. No separate `domain/plugin_installs` module — VCS lives as columns on `orgs`; coding agents are rows in `org_coding_agents` owned by the orgs service.

### Touched (lighter)

- `domain/identity` (users service) — adds `github_username` field + endpoint to update it via the verify-only OAuth flow.
- `plugins/oauth_github` — callback writes `users.github_username` on every successful login, in addition to creating/updating the `oauth_identities` row.
- `core/registries` (or wherever plugin enumeration lives today) — exposes filtered listing by `PluginType` to a new `/api/plugins/available?type=vcs` endpoint.
- Existing GitHub App install handlers from M02 — refactor so they're invoked via `domain/orgs.set_vcs` rather than directly. Keeps the install handshake but routes through the orgs service.
- `plugins/claude_code` — exposes default orchestrator config + default sub-agent set as Python constants, plus a `get_defaults()` accessor consumed by the dedicated defaults endpoint. Reads its runtime settings from `org_coding_agents.settings` JSONB via `domain/orgs.list_coding_agents`. Reads its API key from `core/byok.get(org_id, "anthropic")`. Provides the `validate` callable that `core/byok` invokes for Anthropic-specific test-key calls.

## Plugin contract

- Existing `PluginMeta` (`core/primitives`) is reused unchanged: `id`, `type`, `display_name`, optional `description`, optional `docs_url`.
- Each plugin additionally exposes (new contract):
  - `settings_schema()` — returns a JSON-Schema-like descriptor (just enough for form rendering: field name, label, type, required, enum, help text).
  - `install_url(org_id) -> str | None` — optional. If the plugin needs an external OAuth-style install handshake (like the GitHub App), returns the URL to redirect to. None means settings are pure-form and stored directly.
  - `validate_settings(settings: dict) -> dict | raises` — server-side validation before persist.
- The registry produces lists filtered by `PluginType.VCS` and `PluginType.CODING_AGENT`. UI never hardcodes plugin ids.

## Data model

New tables / columns:

- `users.github_username` — nullable text. Migration adds the column; `oauth_github` updates it on login.
- `orgs.session_timeout_override` — nullable integer (minutes).
- `orgs.vcs_plugin_id` — nullable text (plugin id). `orgs.vcs_settings` — nullable jsonb. Single chosen VCS per org. Co-located on `orgs` rather than its own table because there's at most one row.
- `org_coding_agents` — PK `(org_id, plugin_id)`, `settings jsonb not null default '{}'`, `created_at`, `updated_at`, `created_by uuid not null` (references `users.id`). Many per org. For `claude_code`, the JSONB shape is `{orchestrator: {name, prompt, model, version, effort, updated_at}, agents: [{name, prompt, model, version, effort, updated_at}, ...]}`. Sub-agent name uniqueness within `agents[]` enforced by a Pydantic validator on the request model.
- `byok_keys` — PK `(org_id, provider)`, `encrypted_value text not null`, `last_validated_at`, `last_used_at`, `created_at`, `updated_at`. M03 ships with `provider = "anthropic"` only.

GitHub installations table from M02 (`github_installations(org_id, installation_id)`) is unchanged; it's the implementation detail of the github VCS plugin's `vcs_settings`.

Migration follows the project pattern: a new named entry in `core/database/service.py:_MIGRATIONS` (`011_create_all_m03` or the next-available number).

## Routing

### Web

- `/account` → redirect to `/account/details`.
- `/account/details`, `/account/security`.
- `/orgs/{slug}/settings` → redirect to `/orgs/{slug}/settings/auth`.
- `/orgs/{slug}/settings/{section}` where section ∈ `auth | members | vcs | coding-agents | audit`.
- Dashboard / Tickets / Memory stay at their existing `/orgs/{slug}/...` paths.

### API

New endpoints (all under flat `/api/...`, with `X-Org-Slug` for org-scoped ones):

- `GET /api/account/me` — already exists from M02; extend payload with `github_username`, per-org handle list.
- `PATCH /api/account/me` — update `display_name`, `github_username`-clear (clear only; setting it goes through the verify flow).
- `PATCH /api/memberships/me/{org_id}` — update `handle` for the current user in a specific org.
- `GET /api/account/github/verify` — start verify-only OAuth flow. Returns redirect URL.
- `GET /api/account/github/verify/callback` — finishes verify-only flow, writes `users.github_username`.
- `PATCH /api/orgs/{slug}` — update `session_timeout_override` (Owner/Admin only).
- `GET /api/plugins/available?type=vcs|coding_agent` — registry-driven enumeration. Returns `[{plugin_meta, settings_schema}, ...]`.
- `GET /api/orgs/{slug}/vcs` — current VCS install state.
- `POST /api/orgs/{slug}/vcs` — set VCS (body: `{plugin_id, settings}` or initiates the plugin's `install_url` flow).
- `DELETE /api/orgs/{slug}/vcs` — clear VCS.
- `GET /api/orgs/{slug}/coding-agents` — list installed.
- `POST /api/orgs/{slug}/coding-agents` — install (body: `{plugin_id, settings}`).
- `PATCH /api/orgs/{slug}/coding-agents/{plugin_id}` — update settings.
- `DELETE /api/orgs/{slug}/coding-agents/{plugin_id}` — uninstall.
- `GET /api/orgs/{slug}/coding-agents/claude_code/defaults` — returns code defaults (orchestrator + sub-agents). **Not** returned by the main GET; UI fetches separately to render reset-to-default + overridden badges.
- `GET /api/orgs/{slug}/byok` — list providers with status (configured / not set), timestamps, no plaintext.
- `POST /api/orgs/{slug}/byok/{provider}` — set/update key.
- `POST /api/orgs/{slug}/byok/{provider}/validate` — call provider to verify key.
- `DELETE /api/orgs/{slug}/byok/{provider}` — clear.

All mutations emit audit-log entries (`vcs.installed`, `vcs.removed`, `coding_agent.installed`, `coding_agent.claude_code.settings_saved`, `byok.set`, `byok.cleared`, `byok.validated`, etc.). For Claude Code settings saves: one audit entry per save action (not per field).

## Verify-only GitHub OAuth flow

Separate from the login-via-GitHub OAuth flow in M02. Same Authlib client, different callback handler:

- Login flow stores the identity in `oauth_identities` and issues a session.
- Verify flow runs OAuth, reads the username from the response, writes only `users.github_username`. No identity row touched. No session issued (user already has one).

Implemented as a second pair of endpoints in `domain/account` (not in `plugins/oauth_github` directly — the plugin exposes the OAuth client; the verify-flow handler in `domain/account` orchestrates it).

## Sidebar component model

- Single `<Sidebar>` component reads a static nav config (top-level items + sub-items).
- Nav items typed: `{kind: "link" | "group", label, icon, path?, role?, children?}`. `group` items expand; `link` items navigate.
- Role-gated items (`role: "admin"`) hidden when current membership lacks the role.
- Collapse state stored per group in `localStorage`; restored on mount.
- Bottom user card is a separate component rendering `useCurrentUser()` output + a popover with User > Details / Security / Log off.

## Permissions enforcement

- Frontend: `<RequireMembership role="admin">` (from M02) wraps Org Settings sub-routes that need it. Sidebar items also check role to hide nav for non-admins.
- Backend: each new endpoint declares its `require(action)` dependency from `domain/auth` per M02's contract.

## Migration story

- All M03 changes are additive at the data layer (new columns, new tables). No destructive migration.
- Frontend route changes are not backward-compatible. M02 routes that this milestone re-homes are deleted, not redirected at the application layer (browser bookmarks would 404). Acceptable since both milestones land before any users exist outside the dev team.

## Risks

- **Plugin registry coupling**: today's registry is implicit (plugins import themselves at module load). The picker needs an explicit listing API. If the registry has no enumeration method yet, this milestone adds one — a small refactor that touches every plugin.
- **VCS swap during active reviews**: removing the org's VCS while reviews are running is undefined behavior. M03 doesn't address this; assume the user knows what they're doing. Worth a TODO for M04+.
- **Verify-only OAuth callback URL**: shares the registered GitHub OAuth App, so the App must allow both callback URLs (login + verify). Same App, two paths, no extra registration.
- **Claude Code settings UI is hard-coupled to backend shape**. Refactoring the JSONB shape later means a coordinated frontend + backend change. Acceptable given monorepo + first-party plugins; flagged so it's not a surprise.
- **Sub-agent name uniqueness enforced only at the API layer (Pydantic validator)**, not in the database. A direct INSERT bypassing the API could produce duplicates. Acceptable since all writes go through the service layer in our application; no external writers.
- **Defaults endpoint can drift from live defaults**. The plugin-code defaults are imported at request time, so they always reflect the current binary — no drift risk if the endpoint is wired correctly. But if a future contributor caches them, override badges may lie. Note in `plugins/claude_code` docs.
