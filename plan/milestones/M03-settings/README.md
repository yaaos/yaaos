# M03 — Settings + sidebar restructure

> Restructures the left rail into collapsible sections; introduces User and Org Settings as top-level groups; adds VCS and Coding Agent setup, GitHub-handle association for SSO users, and per-org session-timeout override.

## Status

`[planned]` — sequenced after M02. Reworks UI routes M02 introduced; depends on M02's identity + orgs + audit + SSO modules existing.

## Reading order

1. [requirements.md](requirements.md) — locked spec: nav structure, User section, Org Settings section, data model additions.
2. [architecture.md](architecture.md) — module layout, plugin-picker shape, routing, sidebar component model.
3. [implementation-plan.md](implementation-plan.md) — phased build order.

## Scope at a glance

- **Sidebar**: collapsible top-level sections + sub-items, max two levels deep. User card at the bottom.
- **User section** (under `/account/`): Details (handle per-org, GitHub association, display name, emails), Security (TOTP), Log off (signs out all sessions).
- **Org Settings section** (under `/orgs/{slug}/settings/`): Auth (SSO + session-timeout override), Members, VCS (one plugin per org), Coding Agents (many plugins per org), BYOK (Anthropic key for M03), Audit log.
- **Top-level org pages stay**: Dashboard, Tickets, Memory.
- **Claude Code plugin gets a bespoke settings page** — orchestrator + sub-agents with collapsible prompts, per-field model/version/effort, reset-to-default + overridden indicators, inline Anthropic key field, 1–8 sub-agent cap, unique sub-agent names. Hard-coupled to backend shape (acceptable: first-party monorepo).
- **New data**: `users.github_username`, `orgs.session_timeout_override`, `orgs.vcs_plugin_id` + `vcs_settings`, `org_coding_agents` (JSONB settings; Claude Code shape: orchestrator + agents array), `byok_keys`.
- **New plugin discovery**: VCS and coding-agent plugins expose metadata so the settings UI can render the picker. Per-plugin settings UIs are bespoke React components (no generic JSON-schema form in M03).

## Out of scope (deferred)

- Multi-VCS per org.
- Per-review coding-agent selection (defaults / per-review picking handled by existing review flow; not exposed in M03 UI).
- Mobile drawer / full small-screen pass.
- Custom roles, deeper org-policy controls beyond SSO + session timeout.
- Plugin marketplace / dynamic plugin loading — picker lists what the binary ships.

## What changes from M02

M02 ships routes at flat paths (`/account`, members page under org settings, SSO config, audit log). M03 re-homes them:

- M02 `/account` → M03 `/account/details` + `/account/security` (split).
- M02 Members page → unchanged location, now reachable via Org Settings > Members in the new sidebar.
- M02 SSO config → moves under Org Settings > Auth alongside session-timeout override.
- M02 Audit log → moves under Org Settings > Audit.

These are URL refactors plus nav rewiring. No behavioral change to underlying services.
