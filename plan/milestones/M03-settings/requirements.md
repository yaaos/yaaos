# M03 requirements

> Locked spec. Changes require explicit milestone amendment.

## Sidebar

- Two-level navigation: top-level items + optional sub-items (single nesting depth).
- Top-level items have an icon + label; expandable items show a chevron and reveal sub-items in place.
- Collapsed/expanded state per top-level item persisted in `localStorage`.
- Active route highlights both the matching sub-item and its parent.
- User card pinned to the bottom of the sidebar: avatar (initials for now) + display name + handle for the current org. Click expands a popover with User section sub-items.
- Sidebar collapses to icon-only on narrow desktop widths. Mobile drawer deferred.

## Top-level nav (in order)

Org-scoped (under `/orgs/{slug}/`):

- **Dashboard**
- **Tickets**
- **Memory**
- **Org Settings** (expandable)
  - Auth
  - Members
  - VCS
  - Coding Agents
  - BYOK
  - Audit

User-global (popover from bottom user card, routes under `/account/`):

- **Details**
- **Security**
- **Log off** (action button; not a page)

## User > Details

- Editable: `display_name`.
- Editable: `handle` **per org** — Details shows a table of `(org name, handle in that org)`; each row has an inline edit. Handle uniqueness is per-org (matches M02 data model).
- Read-only list of verified emails with primary marker. (Email add/verify/remove flows are not in M03 scope unless trivially inherited from M02.)
- **GitHub handle association**:
  - For all users: shows `users.github_username` if set, with a "Connect GitHub" / "Re-verify" button.
  - Clicking runs a one-shot GitHub OAuth flow whose only purpose is to verify ownership. Result: write the verified GitHub username to `users.github_username`. No `oauth_identities` row is created or modified.
  - For users who already log in via GitHub: the field is auto-populated from their OAuth login (M02 GitHub OAuth callback updates this column on every login).
  - For SSO-only users: this is the one place they can attach a verified GitHub handle.

## User > Security

- TOTP enrollment + management UI (re-homed from M02 `/account`).
- "Sign out of all sessions" button (re-homed; same backend endpoint as M02).
- Future security settings (recovery codes, passkeys, etc.) land here. Out of scope now.

## User > Log off

- Single action. Calls `POST /api/auth/logout-all` (the only logout flavor — there is no per-session logout).
- Redirects to login page.

## Org Settings > Auth

- SSO setup (re-homed from M02): IdP metadata upload, SP metadata download, JIT toggle, exempt-Owner picker.
- **Session-timeout override** (new):
  - Single field: idle timeout in minutes (default falls back to global constant in `core/constants.py`).
  - Stored as `orgs.session_timeout_override` (nullable; null = use global).
  - Applies to all members of the org; enforced by the session lookup path checking the org of the current `X-Org-Slug` against the row.
- Visible/editable: Owner + Admin.

## Org Settings > Members

- Re-home of M02 members page. No behavior change.

## Org Settings > VCS

- One VCS plugin per org. Enforced at the data layer (`UNIQUE(org_id)` on the chosen-VCS row, or a single nullable column on `orgs`).
- UI:
  - If none chosen: picker listing available VCS plugins by `PluginMeta`. Each option shows name + description + docs link. "Add" navigates to that plugin's install flow.
  - If one chosen: shows that plugin's settings (currently: GitHub App installation status, repo list, "Reconnect" / "Remove" actions). "Remove" disconnects; user can then pick again.
- GitHub-App install flow (from M02) becomes the github plugin's contribution to this picker. The signed-state install handshake is unchanged.
- Switching = explicit two-step: Remove current, then Add new. No one-click swap.
- Visible/editable: Owner + Admin.

## Org Settings > Coding Agents

- Many coding-agent plugins per org. No global cap.
- Stored in `org_coding_agents` (`org_id`, `plugin_id`, `settings jsonb`, `created_at`, `created_by`). PK `(org_id, plugin_id)`.
- UI:
  - List of installed plugins with name, status, settings link, "Remove" action.
  - "Add coding agent" button → picker of plugins not yet installed for this org.
  - Per-plugin settings page is plugin-specific. Plugins are first-party (monorepo), not third-party, so each plugin can ship its own bespoke React settings page rather than a generic form.
- Per-review agent selection is out of scope for M03; M03 only manages install + settings. Which agent runs on a given review remains controlled by existing review code.
- Visible/editable: Owner + Admin.

### Claude Code plugin settings page

The `claude_code` plugin gets a bespoke settings page. Shape:

- **One-paragraph architecture description at the top.** Explains: Claude Code runs as an orchestrator Claude session that delegates to sub-agents. The orchestrator's prompt sets the overall task. Each sub-agent has its own prompt and runs as a separate Claude session called by the orchestrator. Two sentences. Static copy.
- **Anthropic API key field.** Same underlying record as Org Settings > BYOK (single source of truth in `byok_keys`). Reveal/hide toggle (shows only last 4 chars by default). "Test key" button calls Anthropic to validate before save. If no key set yet, inline editable here — no forced detour to BYOK.
- **Orchestrator section.** Always exactly one orchestrator. Fields:
  - Name (read-only label "Orchestrator" — not user-editable name).
  - Prompt (collapsible big textarea, scrollable, default-collapsed if long).
  - Model dropdown (e.g. Claude Sonnet 4.5, Claude Opus 4.5, Claude Haiku 4.5). Required, never blank.
  - Version (specific version pin or "latest" alias — exact options come from plugin code).
  - Effort (e.g. low / medium / high / max — exact options come from plugin code; selectable values depend on selected model).
  - "Reset to default" button per field.
  - "Overridden" badge per field that differs from default.
  - `updated_at` shown read-only.
- **Sub-agents section.** Zero to eight sub-agents (orchestrator excluded from the cap). **At least one sub-agent is required** — UI prevents deleting the last one. Each sub-agent:
  - Name (user-editable, ≤ 64 chars, unique within this Claude Code install). Uniqueness validated by Pydantic validator on save.
  - Prompt (collapsible big textarea + scrollable, same shape as orchestrator).
  - Model + Version + Effort (same dropdowns as orchestrator).
  - "Reset to default" buttons per field, "Overridden" badges per field — only for sub-agents that originated from code defaults; user-created sub-agents have no defaults.
  - `updated_at` shown read-only.
  - Remove button (disabled if this is the last sub-agent).
- **"Add sub-agent" button.** Disabled when cap (8) reached. New sub-agents start with blank-ish defaults (placeholder prompt, default model config).

Storage: settings live entirely in `org_coding_agents.settings` JSONB for the `claude_code` row. Shape: `{orchestrator: {...}, agents: [{...}, ...]}`. No separate Claude-Code-specific table.

Defaults flow:

- Default orchestrator + default sub-agent set defined as constants in the `claude_code` plugin code.
- At install time, defaults are copied into the org's `org_coding_agents.settings` JSONB. From then on, the DB is source of truth.
- A dedicated endpoint `GET /api/orgs/{slug}/coding-agents/claude_code/defaults` returns the code defaults at runtime. **The regular settings endpoint does not include defaults**, only current values. UI calls the defaults endpoint when rendering reset buttons + override badges.

Audit log: one entry per save action (not per field). Action `coding_agent.claude_code.settings_saved` with metadata listing which top-level sections changed (orchestrator / agents).

UI ergonomics:

- All prompts are collapsible big textareas. Default state: collapsed with a one-line preview. Click to expand. Expanded state: large textarea (e.g. 24+ rows) with internal scroll. Character counter visible when expanded.
- Page works at large widths without sidebars stealing space; prompt area gets generous real estate.

## Org Settings > BYOK

- Bring-your-own-key storage for external LLM providers.
- For M03: Anthropic only. Future providers added by extending the same `byok_keys` table + UI.
- Each row: `(org_id, provider, encrypted_value, last_validated_at, last_used_at, created_at, updated_at)`. PK `(org_id, provider)`.
- Encrypted at rest with the same master key M02 uses for TOTP / SAML SP keys.
- UI:
  - List of providers (just Anthropic for M03) with status badge ("Configured" / "Not set" / "Invalid").
  - Per-provider editor: reveal/hide field, "Test key" button, "Save" button, "Remove" button.
  - Last validated, last used timestamps shown read-only.
- Same key surfaced in Claude Code plugin settings page; writes from either UI update the same row.
- Audit log: `byok.set`, `byok.cleared`, `byok.validated`.
- Visible/editable: Owner + Admin.

## Org Settings > Audit

- Re-home of M02 audit page. No behavior change.

## Plugin metadata contract

- VCS plugins and coding-agent plugins expose a `PluginMeta` (the existing `core/primitives.PluginMeta` value) plus a declarative settings schema (JSON Schema or equivalent) consumed by the UI.
- Plugin discovery: the existing plugin registry exposes filtered lists by `PluginType` (VCS, coding agent). Picker UI consumes those lists; no hardcoded plugin names in UI code.

## Permissions summary

| Page | Who can view | Who can edit |
|---|---|---|
| Dashboard / Tickets / Memory | All members | Per existing behavior |
| Org Settings > Auth | Owner, Admin | Owner, Admin |
| Org Settings > Members | All members (read-only list) | Owner, Admin |
| Org Settings > VCS | Owner, Admin | Owner, Admin |
| Org Settings > Coding Agents | Owner, Admin | Owner, Admin |
| Org Settings > BYOK | Owner, Admin | Owner, Admin |
| Org Settings > Audit | Owner, Admin | n/a (read-only) |
| User > Details | Self only | Self |
| User > Security | Self only | Self |
| Log off | Self | n/a |

Members never see any Org Settings sub-item except Members (and only the listing, not the edit affordances).

## Data model additions

- `users.github_username` — nullable text. Verified-via-OAuth profile field. Updated on every GitHub OAuth login.
- `orgs.session_timeout_override` — nullable integer (minutes). Null falls back to `SESSION_IDLE_TIMEOUT` constant.
- `orgs.vcs_plugin_id` + `orgs.vcs_settings jsonb` — single chosen VCS per org. Or a normalized `org_vcs` row if the settings schema is large; architecture decides.
- `org_coding_agents` — `(org_id, plugin_id) PK`, `settings jsonb`, `created_at`, `updated_at`, `created_by`. Owned by `domain/orgs`. For `claude_code`, the JSONB shape is `{orchestrator: {name, prompt, model, version, effort, updated_at}, agents: [{name, prompt, model, version, effort, updated_at}, ...]}`. Sub-agent name uniqueness within `agents[]` enforced by Pydantic validator at the API boundary.
- `byok_keys` — `(org_id, provider) PK`, `encrypted_value text`, `last_validated_at`, `last_used_at`, `created_at`, `updated_at`. Owned by `core/byok`. Single encryption with the M02 master key.

## Cross-cutting test requirements

- Every new endpoint: triplet test (unauth 401, wrong-org/wrong-role 403/404, success 200) per M02's pattern.
- E2E: log in → switch to a fresh org → set up VCS → add a coding agent → change a handle on Details → connect GitHub identity → check audit log shows the changes.
- Sidebar component: visual regression / snapshot test for collapsed + expanded states.

## Explicit cuts (POC)

- Multi-VCS per org.
- Per-review coding-agent selection UI.
- Mobile drawer.
- Custom roles or beyond-SSO-and-timeout auth controls.
- Email add/verify/remove flows beyond what M02 already ships.
- Plugin marketplace / install-time plugin download.
- Recovery codes, passkeys, hardware-key 2FA.
- Per-review token budgets, per-org concurrency caps. Additive later if needed; not in M03.
- Agent slugs / programmatic name templating in the orchestrator prompt. Display-name uniqueness is enough for POC.
- Agent ordering. Orchestrator picks delegates by meaning, not list order.
- Token usage / cost dashboard. Future milestone with Anthropic usage API integration.
- Per-repo prompt overrides.
