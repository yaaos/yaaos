# M06 requirements

> Filled in section by section as we work through [process.md](process.md). Each section's checkbox flips when its content is locked here.

## Progress

- [x] A1. Users + jobs-to-be-done
- [x] A2. Surface inventory
- [x] A3. Mental model
- [x] B1. Information architecture
- [x] B2. Page archetypes
- [x] B3. Navigation model
- [x] C1. Standard UX flows
- [x] C2. State patterns (empty / loading / error / success)
- [x] C3. Information density
- [x] D1. Component library decision
- [x] D2. Design tokens
- [x] D3. Iconography + voice
- [x] D4. Accessibility baseline
- [x] E1. Per-surface design pass — priority + scope
- [x] E2a. Per-surface information design (anchors + Tier 2 + Tier 3 all locked)
- [x] E2b. Per-surface visual design (Claude Design mocks for all 4 anchors)
- [x] F1. Implementation slicing
- [x] F2. Definition of done

---

## A1 — Users + jobs-to-be-done

Three user types. Same surfaces, role-gated affordances — not separate route trees.

### Admin

Sets up + operates yaaos for the team. Absorbs the Operator role (no separate user type).

1. Get yaaos working end-to-end for the team (one-time, high-stakes).
2. Configure plugins (intake, coding agents, reviewers, VCS) + policy.
3. Manage members and what they can do.
4. Spot failing agents, workspaces, or workflows ("is anything on fire?").
5. Investigate why a coding agent didn't behave as expected (agent-level troubleshooting; platform-level failures route to the vendor, not the SPA).
6. Watch budget burn and set caps.
7. Audit who did what.

Note: 7 jobs (over the 3–6 suggested cap) — accepted because Admin genuinely wears two hats (config + ops). IA implication: Settings is the densest surface area.

### Builder

Anyone in the org who isn't an Admin. Covers engineers, SREs, platform engineers, senior reviewers, AND non-engineering teammates who trigger work via intake (Slack, ops alerts, Linear). One role, democratized — "Builder" is the industry term for people who build software regardless of whether they're traditionally engineers.

Absorbs the previously-considered Engineer + Reporter roles. All Builders have full action access (ack findings, push back, re-trigger, teach lessons, configure their own messaging). The earlier read-only Reporter tier is dropped.

Jobs (ordered by frequency, all available to every Builder):

1. See what's happening on my PR / my tickets (default landing).
2. Find a specific ticket I heard about (Slack ping, teammate mention, my own report) — drives search + URL-share-ability.
3. Respond to a finding (ack, push back, request re-review after fix).
4. Get pulled into someone else's HITL'd ticket and resolve it.
5. Teach yaaos that a finding is wrong (lessons; institutional memory).

Common subsets in practice:
- In-team engineers do all 5 frequently.
- Slack/Linear/ops-alert triggerers mostly do 1 and 2; occasionally 3.
- Same role, same affordances; usage patterns vary.

IA implication: one unified Tickets list with filtering — no separate "My Tickets" vs "All Tickets" surfaces.

### Cross-type implications for later sections

- **Two roles only: Admin and Builder.** Adds `builder` alongside M03's `admin` / `member` (the `member` role from M03 maps to `builder` in M06's vocabulary).
- **Same surfaces for both roles.** No role-gated routes. Admin gets extra affordances on settings pages; Builders see all org-scoped surfaces but can't mutate org-wide config.
- **Settings will be the densest area** (Admin job count). Expect a multi-section settings IA.
- **Tickets list is the universal landing for Builder work.** One list, filtered — never split per persona.
- **Deep-linking matters.** Builder job 2 depends on every ticket having a stable, shareable URL.

---

## A2 — Surface inventory

Flat inventory; grouping deferred to B1. Surfaces are tagged Exists / M05-extended / M05-new / Proposed-new.

### Pages

| # | Surface | Route | State | One-liner |
|---|---|---|---|---|
| 1 | Login | `/login` | Exists | Email/password + SSO entry. |
| 2 | Dashboard | `/orgs/:slug/dashboard` | Exists | Two-state: onboarding/setup OR populated metrics + in-flight. |
| 3 | Tickets list | `/orgs/:slug/tickets` | Exists | All tickets, filterable. |
| 4 | Ticket detail | `/orgs/:slug/tickets/:id` | Exists + M05-extended | Review card, findings, Teach-yaaos action, plus M05's workflow view + activity stream + inline HITL prompt. |
| 5 | Memory | `/orgs/:slug/memory` | Exists | Per-repo lessons CRUD. |
| 6 | Settings — Auth | `/orgs/:slug/settings/auth` | Exists | SSO + session-timeout. |
| 7 | Settings — Members | `/orgs/:slug/settings/members` | Exists | Invite, role-change, remove. |
| 8 | Settings — Audit | `/orgs/:slug/settings/audit` | Exists | Audit log. |
| 9 | Settings — VCS | `/orgs/:slug/settings/vcs` | Exists | GitHub App install + repo config. |
| 10 | Settings — Coding Agents list | `/orgs/:slug/settings/coding-agents` | Exists | Installed plugins + add. |
| 11 | Settings — Coding Agent detail | `/orgs/:slug/settings/coding-agents/:pluginId` | Exists | Per-plugin bespoke settings. |
| 12 | Settings — BYOK | `/orgs/:slug/settings/byok` | Exists | Anthropic API key. |
| 13 | Settings — Integrations | `/orgs/:slug/settings/integrations` | Exists | Linear / Notion OAuth. |
| 14 | Settings — Workspace provider | TBD (under settings) | M05-new | `in_memory` vs `remote_agent` provider config. |
| 15 | Account — Details | `/user/details` | Exists | Display name, per-org handle, GitHub handle, emails. |
| 16 | Account — Security | `/user/security` | Exists | TOTP + sign-out-all. |
| 17 | Org picker / switcher | Topbar dropdown (no route) | Proposed-new | Cross-org switcher. |
| 18 | Notifications popover | Topbar bell (no route) | Proposed-new | Latest N notifications across all orgs. |
| 19 | Notifications full page | `/notifications` | Proposed-new | Cross-org notifications, filters, mark-read. **User-scoped, not org-scoped.** |

### Overlays (not pages)

- Teach-yaaos modal (from Ticket detail).
- VCS config dialog (from Settings — VCS).
- More overlays defined in B2/C1 once modal-vs-page conventions are locked.

### Cuts (out of M06 scope)

- Budget / cost view — deferred.
- Agent investigation surface — deferred; basic activity stream inside Ticket detail suffices.
- Workspace status as a per-ticket UI element — removed; absence of a healthy workspace trips the org's setup-required gate (see carry-forward).

### Carry-forward into later sections

- **B3 navigation model** must define the "org not configured → app gated to setup" rule. Threshold: VCS plugin + ≥1 coding agent + valid BYOK (or equivalent set). Builders landing on a non-configured org see "ask your admin to finish setup"; Admins see the setup checklist.
- **B1 IA** must accommodate user-scoped vs org-scoped split. Notifications (#18, #19) and Org picker (#17) are user-scoped (topbar); everything else is org-scoped (sidebar). Account (#15, #16) is also user-scoped but reached via the User card in the sidebar per M03 — confirm in B1.

---

## A3 — Mental model + terminology

The product's nouns and how they nest in a user's head. Drives URL structure, breadcrumb shape, and UI vocabulary.

### Noun hierarchy

```
User                                    (user-scoped, cross-org)
  ├─ Notifications                      (cross-org notification list)
  ├─ Account
  │   ├─ Details                        (name, handles, GitHub, emails)
  │   └─ Security                       (TOTP, sign-out-all)
  └─ Messaging                          (Slack, Telegram, Email — future surface)

Org                                     (the user's current container)
  ├─ Repo                               (first-class noun; sibling to Ticket)
  ├─ Ticket                             (first-class noun; sibling to Repo)
  │   ├─ Stage(s)                       (intended stages declared up-front; shown even when only one)
  │   │   ├─ Attempt(s)                 (numbered if >1, e.g. "Attempt 1 (failed), Attempt 2 (running)")
  │   │   │   ├─ Activity events        (SSE stream of agent activity)
  │   │   │   └─ HITL prompt            (when stage is paused awaiting human)
  │   │   └─ Findings                   (produced within a stage; persist across attempts; first-class deep-linkable noun)
  ├─ Lessons                            (org-scoped, optional repo filter; page renamed from "Memory")
  └─ Org Settings
      ├─ Auth
      ├─ Members
      ├─ Audit
      ├─ VCS
      ├─ Coding Agents
      ├─ MCP Proxy                      (formerly "Integrations" — Linear/Notion via MCP)
      └─ API Keys                       (formerly "BYOK")
```

### User accounts + roles

Both roles (Admin / Builder) get full yaaos accounts. Account, Security, and Messaging settings are universal. All Builders have full action access on org-scoped surfaces (ack findings, teach lessons, re-trigger). Only Admins can mutate org-wide settings (plugins, members, BYOK / API Keys, etc.).

Per-component role checks in React for Admin-only mutate actions. No anonymous-magic-link path.

### Terminology — locked

| Term | Use in UI | Backend equivalent |
|---|---|---|
| **Org** | Container | `orgs` |
| **User** | Person | `users` |
| **Repo** | First-class noun, sibling to Ticket | `repos` |
| **Ticket** | First-class noun, sibling to Repo | `tickets` |
| **Stage** | User-facing phase of a ticket | Workflow Execution (one per stage) |
| **Attempt** | Numbered retry within a stage; shown when >1 exists | Workflow Execution retries |
| **Activity / Activity event** | Streamed events from running agent | `core/sse_pubsub` ActivityEvent |
| **Finding** | Agent-produced artifact, first-class deep-linkable noun | `findings` |
| **Lesson** | Institutional memory entry (page renamed from "Memory") | `lessons` |
| **HITL** | The "decision needed" prompt category — kept as acronym for technical-audience precision | `pending_human_decisions` |
| **Coding Agent** | Plugin type (Claude Code etc.) | coding agent plugin |
| **VCS** | Plugin type (GitHub) | vcs plugin |
| **MCP Proxy** | Plugin type (Linear, Notion via MCP) — chosen for precision over softer alternatives | integrations / MCP proxy |
| **API Keys** | Org-scoped LLM keys (page renamed from "BYOK") | byok_keys |
| **Messaging** | User-scoped notification destinations (Slack, Telegram, Email) — future surface | future |
| **Member** | Person inside an org | `org_memberships` |
| **Admin / Builder** | The two roles. "Builder" is the industry term for people who build software regardless of engineering background; absorbs the previously-considered Engineer + Reporter roles. **Automated (non-human) triggers — scheduled scans, ops alerts, etc. — render as "yaaos" with its logo wherever a Builder identity would otherwise appear.** | `admin` / `builder` (was `member`) |
| **Notification** | Cross-org inbox item | future |

### Banished from UI (backend-only)

`Workflow`, `WorkflowExecution`, `WorkflowCommand`, `AgentCommand`, `Workspace` (the M05 sandbox — UI never uses this word for anything else either, to avoid collision), `Run`, `Job`, `Plugin` (the umbrella), `Task` (the taskiq one), `BYOK`, `Integration` (replaced by MCP Proxy).

### Carry-forward into later sections

- **Slack architecture context (for future intake/messaging milestone, not M06):**
  - Slack-as-intake (org-scoped): one Slack app installed in the org's Slack workspace; reads channel messages, mentions, etc. Lives under Org Settings → Intake Sources when shipped.
  - Slack-as-messaging (user-scoped): per-user opt-in to receive DMs from yaaos. Lives under User Settings → Messaging when shipped.
  - Same Slack app can serve both flows: workspace install grants channel-read + bot-DM ability for in-workspace users; per-user OAuth covers users whose Slack identity is in a different workspace.
- **Slack→Builder resolution (deferred):** when an intake comes from Slack, yaaos maps Slack identity → yaaos user. Fallback policy for unmatched identities is a future-milestone concern.
- **B1 must accommodate** the user-scoped (Notifications, Account, Messaging) vs org-scoped (everything else) split. Topbar is the natural home for user-scoped affordances; sidebar is org-scoped.

---

## B1 — Information architecture

Locked sitemap, URL structure, and IA rules.

### Sidebar (org-scoped)

```
[Org switcher chip — top of sidebar, current org name + dropdown]

Dashboard
Tickets
Lessons
Settings                             (expands inline; one level deep)
  ├─ Auth
  ├─ Members
  ├─ Audit
  ├─ VCS
  ├─ Coding Agents
  ├─ Workspace
  ├─ MCP Proxy
  └─ API Keys

[User card — pinned bottom → popover, peer items]
  Details
  Security
  Messaging
  Log out
```

### Topbar (user-scoped, cross-org)

```
[ ............................. ][ 🔔 Notifications ][ User avatar ]
```

Minimal because the org switcher is in the sidebar.

### URL structure

```
/login

/orgs                                              org picker; redirects to last-used
/orgs/:slug                                        redirects to /orgs/:slug/dashboard
/orgs/:slug/dashboard
/orgs/:slug/tickets
/orgs/:slug/tickets/:ticketId
/orgs/:slug/tickets/:ticketId/findings/:findingId  deep-link to a finding
/orgs/:slug/lessons
/orgs/:slug/settings/auth
/orgs/:slug/settings/members
/orgs/:slug/settings/audit
/orgs/:slug/settings/vcs
/orgs/:slug/settings/coding-agents
/orgs/:slug/settings/coding-agents/:pluginId       reached via list, not sidebar
/orgs/:slug/settings/workspace                     M05-related
/orgs/:slug/settings/mcp-proxy                     renamed from /integrations
/orgs/:slug/settings/api-keys                      renamed from /byok

/user/details                                   singular `/user` matches the locked term and GitHub-API precedent; the user is a singleton, not a collection
/user/security
/user/messaging                                 placeholder; routing in place even before the feature ships

/notifications                                     user-scoped, cross-org
```

### Renames from current SPA

| Old | New |
|---|---|
| Memory page (label + sidebar item) | Lessons |
| `/orgs/:slug/memory` | `/orgs/:slug/lessons` |
| Integrations page (label + sidebar item) | MCP Proxy |
| `/orgs/:slug/settings/integrations` | `/orgs/:slug/settings/mcp-proxy` |
| BYOK page (label + sidebar item) | API Keys |
| `/orgs/:slug/settings/byok` | `/orgs/:slug/settings/api-keys` |

### IA rules (locked)

1. **Max one nesting level in the sidebar.** Settings expands; sub-items don't expand further. Beyond that, navigation happens inside the page.
2. **User-scoped routes never carry `/orgs/:slug`.** `/user/*` and `/notifications` are cross-org.
3. **Org-scoped routes always carry `/orgs/:slug`.** No org-scoped surface at a flat path.
4. **Repos are first-class but have no top-level nav.** Reached via filters inside Tickets and Lessons; deep-linkable in URLs for future use.
5. **In-page navigation handles depth beyond the sidebar.** Plugin detail, finding detail, etc. — sub-pages reached by clicking inside their parent surface, not by sidebar nodes.

### Carry-forward into later sections

- **"Not configured" gate (from A2):** wraps this sitemap. When the active org lacks healthy config (threshold: VCS plugin + ≥1 coding agent + valid API key — confirm in B3), non-Dashboard org-scoped sidebar items are disabled with a tooltip. Dashboard shows setup checklist for Admin; "ask your admin" for non-Admin. User-scoped items remain enabled.
- **B2 page archetypes** must define the Settings sub-page layout — clicking "Coding Agents" lands on a list that itself contains navigation to plugin detail; "Workspace" / "MCP Proxy" / "API Keys" each have their own structures.
- **B3 navigation model** must define: org-switcher interaction (dropdown vs full-page picker), user popover layout, narrow-screen sidebar collapse (already in M03 — confirm).

---

## B2 — Page archetypes + bespoke pages

The page-layout catalog. Two parts:

- **Archetypes** — reusable layout templates applied to ≥3 surfaces.
- **Bespoke pages** — single-page designs that get individual design attention rather than being squeezed into an archetype.

The rule for what counts as an archetype is strict: must be reused ≥3 times. Otherwise it's bespoke.

### Archetypes

| # | Name | Used by | Shape |
|---|---|---|---|
| 1 | **List** | Tickets, Lessons, Members, Audit, Coding Agents list, MCP Proxy list | Page header + filter/search bar + table or row-list + pagination. Audit is List (forensic use); not Stream. |
| 2 | **Settings form** | Auth, VCS, Workspace, API Keys, Coding Agent detail (complex form), User Details, User Security, User Messaging | Page header + form sections + save behavior. Coding Agent detail folds in here — it's a complex settings form, not a generic "detail" page. |

### Bespoke pages

Each gets its own design pass in E2.

| Page | Why bespoke |
|---|---|
| **Dashboard** | Two-state shape (setup-required / populated); only page with both states; setup-required state subsumes what would have been an Empty archetype. |
| **Ticket detail** | The most complex page in the app — live SSE activity stream, multi-pane, role-gated affordances, stage timeline, findings, HITL. Designed individually; not forced into a Detail archetype. |
| **Notifications** | Cross-org chronological feed with date grouping. Only "stream" surface as a primary page. |
| **Login** | Auth shell; one-off, doesn't share the sidebar/topbar chrome. |
| **Org picker** | Sparse landing for multi-org users; appears only at `/orgs` or on initial sign-in. |

### Layout patterns (reusable structural building blocks)

Distinct from archetypes — these are pieces that archetypes and bespoke pages both compose. Catalog of patterns gets locked in D1 alongside the component library; named here so the term has a home:

- **Header bar** — title + status badge + actions; appears on most pages.
- **Tabs** — in-page navigation between related views.
- **Side rail** — right-aligned metadata pane.
- **Drawer** — slide-in panel from screen edge.
- **Filter bar** — search + filter chips.
- **Stream / chronological list with date grouping** — used by Notifications page AND by the activity-events section inside Ticket detail.
- **Setup checklist** — the un-configured Dashboard's primary content.

### Claude Design fit for B2

Once layout patterns and primitives are locked (D1), we use Claude Design to produce anchor mocks: one per archetype (2), one per bespoke page (5). Seven mocks total define the visual specifics of the app.

### Carry-forward into later sections

- **C1 (UX flows)** must define when a flow takes a user to a List archetype vs. a Settings form vs. a Drawer pattern vs. a Modal.
- **D1 (component library)** locks the primitives that build layout patterns and archetypes.
- **E2 (per-surface design pass)** prioritizes the 5 bespoke pages — especially Ticket detail.

---

## B3 — Navigation model

### No topbar

The SPA has no top horizontal strip. All chrome lives in the sidebar. Main content area gains vertical real estate and a cleaner top edge (page header is the highest element).

### Revised sidebar layout

```
┌─────────────────────────┐
│  [yaaos lockup]         │   top — brand identity; expanded: lockup, collapsed: mark only
├─────────────────────────┤
│  [Org switcher chip]    │   defines current org context
├─────────────────────────┤
│  Dashboard              │
│  Tickets                │
│  Lessons                │   org-scoped block
│  Settings ▼             │
│    Auth                 │
│    Members              │
│    Audit                │
│    VCS                  │
│    Coding Agents        │
│    Workspace            │
│    MCP Proxy            │
│    API Keys             │
├─────────────────────────┤
│  🔔 Notifications  [3]  │   user-scoped zone — cross-org
├─────────────────────────┤
│  [User card]            │   user-scoped popover trigger
│   avatar  name          │
│           @handle       │
└─────────────────────────┘
```

Subtle divider separates the org-scoped block from the user-scoped zone at the bottom.

### yaaos logo

- **Top of the sidebar, above the org switcher.** Standard SaaS placement (Linear, Vercel, Notion).
- **Expanded sidebar:** full lockup (mark + wordmark side-by-side).
- **Collapsed sidebar:** mark only (the square symbol; no wordmark).
- **Theme-aware:** light-theme variant on light background, dark-theme variant on dark background.
- **Click target:** navigates to `/orgs/:slug/dashboard` (home for the current org).
- **Assets** live at `plan/milestones/M06-design-refresh/design/assets/logo/` (SVG preferred, PNG @1x/@2x/@3x acceptable). Implementation copies to `apps/web/public/logos/` in Phase 2 of F1.
- **`public/` subdirectory split is by asset category, not by feature:** `logos/`, `favicons/`, and `illustrations/` (if/when we add empty-state graphics). Feature-named folders (`tickets/`, `dashboard/`) tend to rot once assets get reused.
- **File-naming rules** (apply to every asset under `apps/web/public/`):
  - kebab-case, ASCII only. No spaces, uppercase, parens, dates, or versions in filenames — git tracks those.
  - Project prefix first, then descriptor, then variant tokens ordered most-significant first. Theme tokens are explicit (`-light` / `-dark`) — never rely on an implicit default.
  - Raster sizes go in the filename as a numeric suffix (e.g., `-256`). Pick one convention (`-256` vs `@2x`) and keep it consistent; current assets use `-256`.
  - Examples (already shipped in the design folder): `yaaos-lockup-dark.svg`, `yaaos-mark-light.svg`, `yaaos-mark-light-bg-256.png`.
- **SVGO pass on copy.** Phase 2 runs `npx svgo apps/web/public/logos/*.svg` after copying the SVGs from the design folder. Anthropic / design-tool exports typically include editor metadata that ~halves on optimization with zero visual change.
- **Favicons — partial today, completion is a Phase 2 deliverable.** `apps/web/public/favicon.svg` already exists and is byte-identical to `yaaos-mark-light.svg` — keep as-is. Missing siblings to add in `apps/web/public/favicons/`: `favicon.ico` (multi-resolution: 16, 32, 48 — needed for iOS home-screen + older Safari + PWA install), `apple-touch-icon.png` (180×180), `icon-192.png`, `icon-512.png` (PWA-spec sizes; cheap insurance). Optional: `safari-pinned-tab.svg`. Source is the existing `favicon.svg`; generate via `realfavicongenerator.net` or equivalent and check in (not regenerated per build).

### Org switcher

- Inline dropdown anchored to the chip at the top of the sidebar.
- Lists orgs the user has access to, plus a "View all orgs" link that navigates to `/orgs` (full picker page).
- Selecting an org swaps the slug in the URL and reloads org-scoped data.
- Org list at `/orgs` exists as a page for users with many orgs or who want richer metadata; not the primary switching surface.

### User card + popover

- Pinned to the bottom of the sidebar. Avatar (initials for now per M03) + display name on top line + current-org handle on second line.
- Click → popover opens upward.
- Popover items are flat peers with icon + label (card-with-icons style): **Details**, **Security**, **Messaging**, **Log out**.
- No nested submenus (B1 rule: one nesting level only).

### Notifications

- Sidebar row with bell icon, label "Notifications," and unread-count badge.
- Click → small popover anchored to the row showing recent N items + "See all" link to `/notifications`.
- `/notifications` is the canonical full surface — cross-org chronological feed, bespoke page per B2.

### Narrow-screen behavior

- Confirmed from M03: sidebar collapses to icon-only at narrow desktop widths.
- In collapsed mode, the Settings group can't expand inline (no room for sub-item labels). Instead, clicking the Settings icon opens a **flyout** anchored to it, showing sub-items in a popover-like panel.
- Mobile drawer deferred (not in M06).

### No breadcrumbs, no back-links — Pattern A locked

The sidebar is the only back-affordance.

- Detail pages do **not** include `← Back` links in their headers.
- Breadcrumbs do not exist anywhere in the SPA.
- Users return to a parent surface by clicking its sidebar item, which remains highlighted while viewing a child surface.

This locks an IA discipline going forward: every new surface must be designed so that the sidebar context tells the user where they are. If a future surface seems to need breadcrumbs or back-links, the right response is to rethink the IA, not to add the affordance.

### IA-stays-shallow rules

Consequences of Pattern A, written as enforceable design rules:

1. **Sidebar item visibility = parent context.** Every surface reachable via the sidebar must keep that sidebar item visible+highlighted while the surface is displayed.
2. **One drill past a sidebar item, max.** A page reached by clicking *inside* another page (e.g., plugin detail from the plugins list) is allowed, but only one level deep. The sidebar item for the parent remains highlighted.
3. **No drill chains.** If a feature seems to require a child-of-child-of-sidebar-item, redesign the IA.
4. **Deep-links resolve in place.** A URL pointing at sub-state (e.g., a finding within a ticket) opens the parent surface with the sub-state in focus (scroll/expand/highlight), not a separate child page.

### "Not configured" gate

**Threshold for "configured":** an org needs all four of —

1. VCS plugin installed AND at least one repo connected.
2. ≥1 Coding Agent configured.
3. Valid API Key (or BYOK equivalent).
4. Workspace provider selected (M05 — `in_memory` defaults satisfy this in dev).

If any is missing, the org is in **"not configured"** state.

**Gate behavior:** functional gate (not strict).

- Reading existing data (past tickets, findings, lessons, audit) is always allowed regardless of config state.
- *Creating* new work is blocked until config is healthy: PR webhooks are accepted but the workflow can't run (returns a clear "yaaos is not configured" error in the ticket).
- **For Admin**: Dashboard shows the setup checklist as its primary content; every other page shows a non-intrusive banner ("Finish setup to start reviewing PRs → ").
- **For Builders**: Dashboard shows "ask your admin to finish setup" with the Admin's name/email; other pages render normally but show an informational banner.
- Triggering a new review action while gated produces an inline error explaining what's missing.

### Carry-forward into later sections

- **C1 (UX flows)** uses Pattern A — no flow design should rely on header back-links or breadcrumbs.
- **B2 Notifications popover** must be designed alongside the bespoke Notifications page (the popover is a layout pattern; the page is the bespoke surface).
- **D1 component library** must include the Org switcher dropdown, User popover, Notifications popover, and Sidebar primitive as composites — these are the chrome.
- **Iconography pass (D3)** locks the icon set; bell + chevron + sidebar-icon-set all draw from one place.

---

## C1 — Standard UX flows

Flow table: for each common user action, the locked pattern.

### Patterns in use

- **Modal** — centered overlay; short decisive actions, small forms, confirms.
- **Page / route push** — full navigation; long forms, deep-linkable surfaces.
- **Inline edit / action** — in-place on the current view; single-field tweaks, quick actions.
- **Popover** — small floating panel anchored to a trigger; menus, simple pickers, peeks.
- **Toast** — corner notification, transient; save confirmations, success/failure flashes.

### Patterns explicitly cut from M06

- **Drawer / side panel** — no action in the table needs it; cutting from the layout pattern catalog in B2.
- **Wizard / multi-step modal** — no flow requires it; if a multi-step shape emerges, revisit.

### Flow table

| Action | Pattern | Notes |
|---|---|---|
| Open a ticket from the list | Page | Already locked. Tickets deep-linkable. |
| Add a coding-agent plugin | Picker modal → Page | Modal lets user pick plugin type (Claude Code today; Codex, Aider later); confirm navigates to `/settings/coding-agents/:pluginId`. Pattern reused for any plugin install. |
| Add an MCP Proxy connection | Picker modal → Page | Same pattern as coding-agent. |
| Install VCS plugin (GitHub App) | Picker modal → Page → external OAuth → return | Picker for future VCS-plugin variants. OAuth flow goes to GitHub and back. |
| Edit a settings field (API key, session timeout, etc.) | Inline form on its settings page | Settings sub-pages own their forms. No modal. |
| Confirm action (destructive OR cost-protective) | Modal confirm | Destructive: delete plugin, remove member. Cost-protective: re-trigger review (spends LLM tokens). Same modal shape; differs only in copy. |
| Re-trigger a review | Modal confirm → action → toast | Cost-protective confirm before spending tokens. |
| Teach yaaos (push back / add lesson from a finding) | Modal | Already in M03 SPA; keep. |
| Create a lesson from scratch | Modal | Short form, no deep-link need. |
| Edit a lesson | Modal | Same shape as create. |
| Ack / respond to a finding | Inline action on the finding row | Short, in-context. |
| Respond to a HITL prompt | Inline panel within Ticket detail | The prompt is already foregrounded — response stays in place. |
| Switch orgs | Inline dropdown (org switcher) | Locked B3. |
| View notifications (peek) | Popover from sidebar bell | Locked B3. |
| View all notifications | Page (`/notifications`) | Locked B3. |
| Invite a member | Modal | Short form (email + role). |
| Edit member role | Inline dropdown in the row | One-field tweak. |
| Remove a member | Modal confirm | Destructive. |
| Create a new org | Modal | Short form (name + slug); meaningful onboarding happens via the setup checklist on the new org's Dashboard, not in the create flow. |
| Save confirmation (any settings save) | Toast | Standard. |
| Form validation error | Inline next to field | Field-level. |
| Unexpected error after action | Toast | Surprise errors as toasts. |
| Open a finding deep-link from Slack | Resolve to Ticket detail (anchor-style) | Locked B1. |

### The picker-modal pattern (named)

Because plugin installation is the same shape across plugin categories (Coding Agent, MCP Proxy, VCS), name this pattern explicitly:

**Picker modal → detail page.** Click "Add X" → modal opens listing available X-types → user picks one → modal closes, navigate to detail page for that X-type → user fills in config → save.

Forward-compat: when a second coding-agent plugin (Codex) ships, no new flow design is needed — the picker modal lists two options instead of one.

### Carry-forward into later sections

- **C2 (state patterns)** must define what the picker modal shows when no plugin types are available (shouldn't happen at runtime, but defensive).
- **D1 component library** needs: Modal primitive (with header/body/footer slots, confirm variant), Toast primitive + manager, Popover primitive, Inline-edit primitive (with view/edit modes).
- **D3 voice** must lock the copy patterns for confirm modals — destructive ("This will delete X. Continue?") vs cost-protective ("Re-running this review will spend ~$N in tokens. Continue?").

---

## C2 — State patterns

One designed pattern per state, reused across all surfaces. Pessimistic updates by default — UI reflects state after server confirms (no optimistic flips in M06).

### Empty state

Used when a list, panel, or chart has zero rows (data IS the empty set; distinct from loading).

Spec:
- Centered within the container.
- Icon / simple illustration (~64px, muted).
- Headline — one short sentence ("No tickets yet.").
- Body — one line explaining why / what to do.
- Primary action (optional) — button to do the thing that would make this non-empty.

### Loading state

Two sub-patterns by context:

- **Skeleton** — preferred for List, Settings form initial load, Ticket detail initial load. Gray placeholder blocks shaped like the final content; layout doesn't jump.
- **Inline spinner** — for action-triggered loads. Replaces button label/icon. Disable the action while in flight.

Cut: full-page spinner.

### Error state

Four sub-patterns by context:

- **Inline field error** — form validation. Red text below field, red border, AA-contrast indicator.
- **Banner error** — in-page errors that don't break the page. Sticky banner at top of affected section. "Couldn't load X. [Retry]"
- **Toast error** — unexpected errors after user-triggered actions. Corner toast, red border/icon. Auto-dismiss ~6s, manually dismissible.
- **Full-page error boundary** — catastrophic JS errors (route crashed). Bespoke "something went wrong" page with [Reload] · [Go to Dashboard].

**Stack overlap rule:** never show the same error in two places at once. If a field validation fails, show inline; do NOT also toast.

### Success state

- **Default: toast.** Corner toast, neutral/positive border/icon. "Lesson saved." Auto-dismiss ~4s.
- **When NOT to toast:** if the success is visible directly in the UI (a row updates immediately, the saved field shows its new value), skip the toast.
- **Inline success indicator** rarely used; reserve for forms where the user might miss a toast (e.g., briefly show a check next to the saved field AND a toast).

### Update model — pessimistic

UI does NOT flip until server confirms. No optimistic updates in M06 (simpler; matches the toast-on-success pattern; honest about latency). Future milestones may introduce optimistic on a per-action basis where it matters.

### Live-stream states (SSE — Ticket detail activity pane)

Composes the building blocks above:

| Stream state | Pattern |
|---|---|
| Connecting | Skeleton activity rows OR small "Connecting…" indicator |
| Connected, no events yet | Empty-state component scoped to the activity pane |
| Connected, events flowing | Live render |
| Disconnected | Banner above the activity pane: "Activity stream disconnected. [Retry]" |

### Setup-required Dashboard — exception

The bespoke Dashboard's setup-required state uses the empty-state shape (icon + headline + body) but with structured checklist content rather than a single CTA. Designed individually in E2 alongside the populated Dashboard.

### Carry-forward

- **D1 component library**: need primitives for EmptyState, Skeleton, ErrorBanner, Toast, ErrorBoundary, plus the inline field-error pattern in form primitives.
- **D2 design tokens**: define muted-icon color, skeleton-shimmer color, toast colors (success/error/info), inline field-error red.
- **D3 voice**: lock the copy for empty-state headlines, error retries, success toasts — short, plain, action-suggesting.

---

## C3 — Information density

**Locked: compact density. No user-configurable toggle in M06.**

### Concrete values

| Knob | Value | Notes |
|---|---|---|
| Base body font size | **13px** | Already in `apps/web/src/styles.css`; matches Linear/Datadog convention. Verify AA contrast in D4. |
| Body line-height | 1.5 | Tight enough for density, loose enough to read. |
| Heading line-height | 1.3 | |
| List table row height | 36–40px | Dense rows for forensic surfaces (Tickets, Audit). |
| Card-list row height | 40–48px | Richer per-row content (Lessons, Coding Agents). |
| Page outer padding | 24px (Tailwind `p-6`) | |
| Form field gap | 12px (`gap-3`) | |
| Section gap | 32px (`gap-8`) | |
| Spacing scale base unit | 4px | Tailwind default; aligns with compact mode. |

### Density discipline by surface type

- **Information-bearing surfaces** (List, Ticket detail main pane, Activity stream): tight, dense.
- **Moment-of-action surfaces** (modals, empty states, error states): generous padding regardless of density. ~24px internal padding in modals.
- **Settings forms**: middle ground — compact fields but keep ≥12px field-gap and breathing room around section headers.

### Carry-forward

- **D2 design tokens**: locks the full type scale, spacing scale, radius scale informed by these density values.
- **D4 accessibility**: verify AA contrast at 13px body across all token combinations (light + dark themes).
- **E2 surface design**: Lessons list needs careful per-row design — compact row height that still handles 2–3 lines of prose comfortably.

---

## D1 — Component library decision

**Locked: shadcn/ui** (React primitives copied into our repo, wrapping Radix UI for behavior, styled via Tailwind).

### Why shadcn/ui

- Components are **copied into our codebase** (CLI: `npx shadcn@latest add <component>`), not imported as a versioned package. We own the code, edit freely, no upgrade churn.
- Built on **Radix UI primitives** — focus management, keyboard nav, ARIA roles correct out of the box.
- Ships **opinionated Tailwind defaults** — modern, clean, neutral. No visual differentiation needed; we adopt the defaults and tweak via tokens.
- 2026 industry standard for React + Tailwind apps. Strong community, large library of available primitives, well-documented patterns.

### Initial primitive install set (18)

**Form**: `button`, `input`, `textarea`, `select`, `checkbox`, `switch`, `label`, `form` (with `react-hook-form`).

**Overlays**: `dialog`, `popover`, `dropdown-menu`, `tooltip`.

**Display**: `table`, `badge`, `avatar`, `separator`, `skeleton`, `tabs`.

**Layout**: `sidebar` (shadcn's primitive — handles collapse, sub-items, group rendering), `collapsible`, `scroll-area`.

**Toast**: `sonner` (separate package, shadcn's current recommendation — replaces the older Toast primitive).

**Cut from M06**: `sheet` (drawers cut in C1), `command` (search palette — future), `carousel`, `date-picker`, `calendar`.

### Composites we build on top

These live in our codebase and compose shadcn primitives. Encode yaaos-specific patterns.

**Chrome (`shared/components/chrome/`)**
- `OrgSwitcher`
- `UserCard`, `UserPopover`
- `NotificationsBell`, `NotificationsPopover`

**Layout (`shared/components/layout/`)**
- `Sidebar` (yaaos's, wrapping shadcn's primitive)
- `PageHeader`
- `Section`
- `EmptyState`
- `ErrorBanner`
- `ErrorBoundary`
- `ConfirmModal` (Dialog variant — destructive + cost-protective copy)
- `PickerModal` (Dialog variant with selectable cards — plugin-picker pattern)

**Domain composites** live in their domain modules:
- `domain/tickets/`: `StageIndicator`, `ActivityStream`, `FindingRow`, `FindingCard`, `HITLPanel`, `ReviewCard`.
- `domain/dashboard/`: `SetupChecklist`, `StatCard`, `InFlightList`.
- `domain/org_settings/`: per-plugin-type settings cards.
- `domain/orgs/`: `OrgPickerCard`.

### Token reconciliation

**Approach: adopt shadcn's variable names, populate with our oklch values.**

shadcn components reference `--background`, `--foreground`, `--primary`, `--destructive`, etc. (HSL by default). We override those CSS variables to oklch values in `styles.css`. Components inherit our look without code edits.

Concrete mapping deferred to D2.

### File structure

```
apps/web/src/shared/components/
  ui/                       shadcn primitives (one file per primitive)
  layout/                   yaaos layout composites
  chrome/                   org/user chrome composites
apps/web/src/domain/<feature>/components/
                            domain-specific composites
```

Existing primitives in `shared/components/` (button, badge, card, dialog, placeholder-page) get replaced with shadcn-installed versions; usages update.

### Locked sub-decisions

- **Form primitive: shadcn `Form` + `react-hook-form`.** Replaces current manual validation. Standard for shadcn apps.
- **Toasts: Sonner.** Newer + sleeker than shadcn's older Toast primitive.
- **Sidebar: shadcn `sidebar` primitive as base, our `Sidebar` composite on top.**
- **Token names: shadcn convention; oklch values populate them.**

### Migration order (informs F1 implementation slicing)

1. `shadcn@latest init`, reconcile token names with D2 outputs.
2. Install 18 primitives + Sonner.
3. Refactor existing 4 hand-rolled primitives to use shadcn versions; update imports.
4. Build chrome composites (Sidebar, OrgSwitcher, UserPopover, NotificationsBell).
5. Build layout composites (PageHeader, EmptyState, ErrorBanner, ConfirmModal, PickerModal).
6. Per-surface redesign in E2, building domain composites as needed.

### Carry-forward

- **D2 tokens** must produce the oklch → shadcn-name mapping table.
- **D3 voice** must lock copy patterns for ConfirmModal (destructive vs cost-protective).
- **F1 slicing** uses this migration order as the foundation.

---

## D2 — Design tokens

Token catalog. Two layers: **primitive tokens** (raw values) referenced by **semantic tokens** (named by role). Components consume semantic tokens; nothing in the SPA references a raw color/spacing value.

Implementation: CSS custom properties in `apps/web/src/styles.css` (extends current file) + Tailwind config aliases. shadcn-compatible variable naming.

### Color — semantic roles

Both light and dark themes ship a complete set; values swap, names stay.

**Background + surface**
- `--background` / `--foreground`
- `--card` / `--card-foreground`
- `--popover` / `--popover-foreground`
- `--muted` / `--muted-foreground`
- `--border`, `--input`, `--ring`

**Brand + interactive**
- `--primary` / `--primary-foreground` — yaaos's brand purple (`oklch(0.72 0.19 295)` dark; `oklch(0.55 0.2 295)` light). Hue may shift slightly for AA contrast; the color stays purple.
- `--secondary` / `--secondary-foreground`
- `--accent` / `--accent-foreground` — shadcn-meaning (hover/highlight surface). Distinct from yaaos's brand color, which lives in `--primary`.

**State**
- `--destructive` / `--destructive-foreground`
- `--success` / `--success-foreground`
- `--warning` / `--warning-foreground`
- `--info` / `--info-foreground`

Values inherit from current `apps/web/src/styles.css` oklch palette. AA-contrast verification deferred to D4.

### Type scale — 7 rungs

| Token | Size | Line-height | Use |
|---|---|---|---|
| `text-xs` | 11px | 1.4 | Captions, badge text, table footnotes |
| `text-sm` | 12px | 1.4 | Helper text, secondary labels |
| `text-base` | 13px | 1.5 | Body (default) |
| `text-lg` | 14px | 1.4 | Emphasized body, h4 |
| `text-xl` | 16px | 1.3 | h3 |
| `text-2xl` | 20px | 1.3 | h2 |
| `text-3xl` | 26px | 1.2 | h1 (page-title-only on large pages) |

Font family: **Geist** (already in styles.css).

### Spacing scale — Tailwind default (4px base), committed rungs

| Token | Value | Use |
|---|---|---|
| `space-1` | 4px | Tightest gaps (icon-to-label) |
| `space-2` | 8px | Button internal padding, badge padding |
| `space-3` | 12px | Form field gap, tight list row padding |
| `space-4` | 16px | Standard inline gap, card padding |
| `space-6` | 24px | Page outer padding, section padding |
| `space-8` | 32px | Section gap |
| `space-12` | 48px | Large vertical separation |
| `space-16` | 64px | Hero / setup-checklist spacing |

**Rule:** no arbitrary spacing values (`p-[7px]`). If not on the list, either add it deliberately or fix the inconsistency.

### Radius scale — 5 rungs

| Token | Value | Use |
|---|---|---|
| `rounded-sm` | 4px | Badges, tags, chips |
| `rounded` | 6px | Buttons, inputs, small cards (default) |
| `rounded-md` | 8px | Cards, panels |
| `rounded-lg` | 12px | Modals, large overlays |
| `rounded-full` | 9999px | Avatars, pills, single-letter tags |

Moderate rounding — modern but not consumer-app-friendly; sharp enough to feel professional.

### Motion — subtle philosophy

| Token | Value | Use |
|---|---|---|
| `motion-fast` | 100ms | Hover, focus |
| `motion-base` | 200ms | Open/close (modals, popovers, dropdowns) |
| `motion-slow` | 400ms | Rare; expanding setup checklist, etc. |

Easings: `ease-out` (default snappy entries), `ease-in-out` (symmetric transitions). No bounce, no spring physics, no orchestrated stagger.

### Elevation — 3 rungs

| Token | Use |
|---|---|
| `shadow-sm` | Card resting state, popover |
| `shadow` | Dropdown menu, tooltip |
| `shadow-md` | Modal, sheet |

No `shadow-lg` / `shadow-xl` — outside our visual language.

### Carry-forward

- **D4 accessibility** verifies AA contrast for every (foreground, background) semantic pair in both themes. If purple primary fails AA at 13px on its foreground, shift hue/lightness within the purple family until it passes.
- **D3 voice / iconography** uses these type tokens for label sizes.
- **E2 surface design** references these tokens; no surface invents its own values.

---

## D3 — Iconography + voice

### Iconography — locked

- **Library:** `lucide-react` (already in SPA; shadcn default).
- **Size rungs:** 16px (inline / table cell / chip), 20px (sidebar items, buttons, input icons), 24px (page headers, empty-state graphics).
- **Stroke weight:** lucide default (1.5px). No per-icon override.
- **Color:** semantic tokens only (`text-muted-foreground`, `text-primary`, `text-destructive`, etc.). Never hard-coded.
- **Custom icons:** allowed when lucide lacks a needed glyph. Match lucide's stroke style and weight.

### Voice — locked

Tone: **closer to clinical, very slightly warm.** Fact-stating, not voice-of-the-app.

**Rules:**

1. Direct. "Save changes" not "Don't forget to save your changes!"
2. Plain. "Add a coding agent" not "Onboard a new agent into your workspace."
3. State facts, don't personify yaaos. "Lesson saved" not "yaaos saved your lesson."
4. Active voice in sentences; bare facts in labels.
5. Second person sparingly. Most button labels are first-person ("Save," "Delete"). When addressing the user, do so naturally, not aggressively.
6. Technical terms acceptable — audience is engineers. HITL, MCP Proxy, finding, lesson, stage all OK.
7. Short over long. Two words beats five.
8. Reduce contractions in formal banners ("cannot" over "can't"). Contractions OK in transient toasts where brevity wins ("couldn't save").
9. No exclamation points. Anywhere.
10. No emoji in core UI. Empty-state graphics are icons, not emoji.
11. No marketing-speak. "Awesome!" / "Let's get started!" / "Pro tip!" banned.
12. Errors blame the system, not the user. "Couldn't save lesson" not "You entered an invalid lesson."

### Paired examples (yes / no)

| Context | Yes | No |
|---|---|---|
| Save toast | "Lesson saved." | "Awesome! Lesson saved." / "yaaos saved your lesson." |
| Confirm modal (destructive) | "Delete this coding agent? It will be removed permanently. This cannot be undone." | "Are you sure you want to permanently delete this coding agent and all of its data?" |
| Empty-state | "No tickets yet. Tickets appear here after GitHub opens a PR for review." | "No tickets yet — they'll show up once we see a PR!" |
| Error toast | "Couldn't save lesson. Try again." | "Oops! Something went wrong while saving." |
| Button label | "Add coding agent" | "+ Add a new coding agent" |
| Filter empty result | "Cannot find any tickets matching these filters." | "Looks like nothing matched your filters." |
| Cost-protective confirm | "Re-run this review? Running again will spend roughly N tokens." | "Want to re-run? It'll cost about N tokens." |
| HITL banner | "yaaos needs a decision before continuing." | "🙋 Your input is requested!" |

### Locked copy patterns

**Confirm modal — destructive:**
- Title: "Delete [thing]?"
- Body: "[Thing] will be removed permanently. This cannot be undone."
- Action: "Delete" (destructive variant)

**Confirm modal — cost-protective:**
- Title: "Re-run this review?"
- Body: "Running again will spend roughly N tokens."
- Action: "Re-run"

**Empty state — standard shape:**
- Headline (5–7 words, present tense): "No tickets yet."
- Body (one sentence; what makes this state change): "Tickets appear here after GitHub opens a PR for review."
- Action (verb-first if present): "Connect a repo"

**Toast:**
- Success (2–4 words, past tense): "Lesson saved."
- Error (5–8 words, fact-stating): "Couldn't save lesson. Try again."

### Carry-forward

- **D4 accessibility**: every icon-only button gets `aria-label` derived from its action verb (e.g., the bell icon labeled "Notifications").
- **E2 surface design**: copy on every surface goes through the rules above. Microcopy is not a polish step — it's part of the design.

---

## D4 — Accessibility baseline

**Locked: WCAG 2.2 Level AA on all surfaces.** AAA is out of scope (too complex; not a market-fit need).

### What shadcn + Radix handle automatically

- Overlay focus trap, Escape-to-close, focus restoration, ARIA roles (Dialog, Popover, DropdownMenu, Tooltip).
- Form-field association (`<label for>`, `aria-invalid`, `aria-describedby` for errors).
- Button keyboard activation (Space + Enter).
- Switch / Checkbox / Radio roles.
- Sidebar keyboard navigation.

### What we add on top

- **Focus ring on every interactive primitive.** Token `--ring`, 2px ring + 2px offset, ≥3:1 contrast against any surface. Never removed without replacement.
- **`aria-label` on icon-only buttons** — verb-first, matches D3 voice (bell → "Notifications"; sidebar toggle → "Collapse sidebar").
- **Live regions** on the SSE activity stream (`aria-live="polite"`) and HITL prompts (`aria-live="assertive"`). Stream-disconnected banner also live.
- **Color is never the only signal.** Severity badges include text. Stage indicator includes label/icon. Filter chips show icon + text.
- **Tab order matches visual order.** No `tabindex` rearrangement; default 0 or explicit -1 only.
- **Skip-to-main-content link** at the top of `<body>`, hidden until keyboard-focused.
- **`prefers-reduced-motion` honored.** Tailwind `motion-reduce:` modifiers on animated components (modals, popovers, dropdowns, drawers — though drawers cut).

### Per-surface gotchas

- **Ticket detail activity stream**: live-region; new events announced; disconnected-banner live.
- **HITL prompt**: focus moves to response panel when prompt arrives if user is on the page; otherwise announced in page header.
- **Org switcher**: keyboard-nav inside popover; Escape closes; focus returns to chip.
- **Sidebar collapse**: flyout opens on click + Enter; Escape closes; arrow keys move through flyout items.
- **Notifications popover**: same patterns; "Mark all read" reachable by keyboard.
- **Setup checklist (Dashboard)**: items navigable in order; completed items carry `aria-label="Step X completed"`.

### Verification

- **Automated**: axe-core (or equivalent) in CI for every page in E2E tests. Violations fail CI.
- **Manual**: keyboard-only navigation pass per surface during E2.
- **Contrast**: every (foreground, background) semantic pair verified in both themes; failures fixed by tuning the token value.
- **Color-blindness simulation**: one pass through critical surfaces (Dashboard, Ticket detail, Notifications, Settings) in a deuteranopia/protanopia/tritanopia simulator.

### Cuts

- WCAG AAA (contrast 7:1, etc.) — too restrictive for dense data displays.
- Specialized screen-reader UX authoring beyond ARIA correctness.

### Carry-forward

- **E2 surface design**: every surface gets a 5-minute axe pass during design and a keyboard pass during E2E test authoring.
- **F1 implementation slicing**: contrast verification + color-blindness sim happens before any phase is considered done.

---

## E1 — Per-surface design pass: priority + scope

E2 split into two passes:

- **E2a — Information design per surface.** Prose + tables describing what data appears, hierarchy, actions, states. Done for every surface (anchors + derived).
- **E2b — Visual design per surface.** Claude Design mocks for the four anchors, based on E2a's locked information.

### Tier 0 — Foundation (substrate; precedes any surface design)

- Token reconciliation: `styles.css` extended with shadcn-named variables populated by oklch values per D2.
- shadcn install: 18 primitives + Sonner; CLI configured.
- Layout composites: Sidebar (with chrome), PageHeader, EmptyState, ErrorBanner, ErrorBoundary, ConfirmModal, PickerModal.
- Chrome composites: OrgSwitcher, UserCard + UserPopover, NotificationsBell + NotificationsPopover.

### Tier 1 — Anchors (E2a info design + E2b Claude Design mocks)

| Surface | Type | Why anchor |
|---|---|---|
| **Dashboard** | Bespoke | Most-touched landing; two-state; locks stat cards / in-flight list / setup checklist |
| **Ticket detail** | Bespoke | Centerpiece; most complex; locks stage indicator / activity stream / findings / HITL panel |
| **Tickets list** | List archetype | Prototypical List; locks density / filter bar / status badges / state patterns |
| **Coding Agent detail** | Settings form archetype | Largest Settings form; locks form section shape / collapsible / overridden indicators |

Mock both major states for Dashboard (setup-required / populated) and Ticket detail (no-HITL / with-HITL). One mock for Tickets list and Coding Agent detail.

### Tier 2 — High-touch derived (E2a info design; no Claude Design)

| Surface | Pattern source |
|---|---|
| Lessons list | Tickets list + card variant |
| Notifications page | Bespoke Stream layout |
| Notifications popover | Composes Popover + Notifications row from page |
| Settings — Auth | Coding Agent detail (simpler form) |
| Settings — Members | Tickets list + inline-edit row |
| Settings — Audit | Tickets list (dense forensic) |

### Tier 3 — Lower-traffic derived (E2a info design, brief)

| Surface | Pattern source |
|---|---|
| Settings — VCS | Settings form + connection-status card |
| Settings — Workspace | Settings form, single-section |
| Settings — MCP Proxy | Coding Agents list pattern |
| Settings — API Keys | Settings form, multi-field |
| User — Details | Settings form |
| User — Security | Settings form |
| User — Messaging | Settings form (placeholder; feature deferred) |
| Org picker | Bespoke (sparse card list) |
| Login | Bespoke (auth shell) |

### Checkpoint between anchors and Tier 2

After Tier 1 anchor mocks (E2b) are reviewed and accepted, hold a deliberate checkpoint before deriving Tier 2 specs. If anchors aren't right, derivation amplifies the wrong choice.

### Cuts

- No Claude Design for Tier 2 / Tier 3.
- No design exploration of multiple stylistic alternatives.
- No iteration past the first anchor-mock review pass — if the mock needs three rounds, the archetype is wrong.

### E2a anchor order

Warm up with easier screens; tackle Ticket detail (hardest) last. Lock most-reused archetypes early so derivation has a stable foundation.

1. **Tickets list** — List archetype (6+ derived surfaces follow this pattern).
2. **Coding Agent detail** — Settings form archetype (8+ derived surfaces follow).
3. **Dashboard** — bespoke; composes List + stat cards + checklist.
4. **Ticket detail** — hardest; benefits from all the above being locked.

---

## E2a — Per-surface information design

Anchors in order: Tickets list → Coding Agent detail → Dashboard → Ticket detail.

---

### E2a.1 — Tickets list

Surface: `/orgs/:slug/tickets`. Prototypical List archetype. Locks density, filter bar, status badges, state patterns for 6+ derived surfaces.

#### Columns

| Column | Width | Content |
|---|---|---|
| **Status** | narrow | Icon + state badge (▶ Running, ⏸ HITL, ✓ Done, ✗ Failed, ⊘ Cancelled) |
| **Title** | flex | Ticket title (PR title for PR-review tickets; varies by ticket type) |
| **Repo** | narrow | Repo name (e.g. `my-org/api-server`) |
| **Stage** | narrow | Current stage label (single "Review" today; multi-stage future) |
| **Findings** | narrow | Count + severity dot if any high-severity (e.g. "3" or "3 ●") |
| **Updated** | narrow | Relative time ("2m ago"); hover tooltip shows absolute timestamp |
| **Builder** | narrow | Avatar + name of the person who triggered the ticket. **For automated (non-human) triggers, render as the yaaos logo + "yaaos" label** — this is the convention for system-triggered work. |

Row click → Ticket detail. Whole row is the affordance. No hover actions; all actions live inside Ticket detail.

#### Filters

Filter bar above the table. URL query params persist filter state (shareable, bookmarkable).

- **Status** — multi-select (Running, HITL, Done, Failed, Cancelled). Default: "All open" (Running + HITL).
- **Repo** — single-select with search; "All repos" default.
- **Builder** — single-select with search; "Anyone" default.
- **Date range** — created-at; presets (Today, This week, This month, Custom).
- **My tickets toggle** — quick filter for "tickets I triggered."

Plus a free-text search box — searches ticket title.

#### Sort

- Default: **Most recently updated** descending.
- Alt sort: click column header to sort by Status, Created, Findings count.

#### Pagination

Load more button at the bottom. First batch: 50 rows. Click → next 50 appended. Filters and sort persist across loads. URL doesn't track pagination state (bookmarks are by filter, not by position).

#### States

| State | Pattern |
|---|---|
| Empty (no tickets in org) | `EmptyState`: "No tickets yet. Tickets appear here after GitHub opens a PR for review." |
| Empty (filtered) | `EmptyState`: "Cannot find any tickets matching these filters." + "Clear filters" action |
| Loading | Skeleton rows (5–8) |
| Error | Banner above table with retry |

#### Role-gated affordances

With M06's two-role model (Admin / Builder), Tickets list has no role-gated affordances. Both roles see the same columns, filters, actions. (The previous Reporter read-only tier was dropped — see A1.)

#### Cancelled tickets

Excluded from default view ("All open" filter). User can explicitly include via Status filter.

#### Details deferred to E2b (visual mocks)

- Exact column widths and breakpoints.
- Density of skeleton rows.
- Filter-chip visual shape.
- Status icon + color pairing.

---

### E2a.2 — Coding Agent detail

Surface: `/orgs/:slug/settings/coding-agents/:pluginId`. Largest Settings form in the app; locks form shape, section organization, override patterns, save behavior for 8+ derived surfaces.

For M06, only one Coding Agent type ships (Claude Code). The detail page is shaped for Claude Code today; future types follow the same archetype.

#### Page header (plugin-level)

- Plugin name + icon
- Status indicator (Configured / Misconfigured / Needs setup)
- **Anthropic API key** — editable field. Same value also appears editable on `/settings/api-keys`. Edits in either location call the same backend API; both pages stay in sync. Slightly unusual pattern but reasonable for this single high-value field.
- Last-modified timestamp
- Save / Discard buttons (sticky on scroll)

#### Section 1: Orchestrator

- **Model** — dropdown (Sonnet / Opus / Haiku variants).
- **Effort level** — dropdown (low / medium / high / max — only when model supports it).
- **System prompt** — `Use default` toggle + textarea. When ON, runtime uses the shipped default; textarea displays the stored DB value (so user can see what their custom was) but is uneditable. When OFF, textarea is editable; that text is used at runtime.
- **Tools enabled** — read-only display ("Tools: Bash, Edit, Read, Write, Grep, Glob, …"). Informational only; backend manages the actual enabled set.

#### Section 2: Sub-agents

Repeatable rows; up to 8 per orchestrator. Each row:

- **Name** — text input, uniqueness validated across sub-agents in this orchestrator.
- **Model** — dropdown.
- **Effort level** — dropdown.
- **System prompt** — `Use default` toggle + textarea (same pattern as Orchestrator).
- **Tools** — read-only display.

Per row: "Remove" (destructive confirm). No reordering (UI order has no semantic meaning).

Page-level: "Add sub-agent" button, disabled at 8.

#### Section 3: MCP context

- Multi-select from configured MCP Proxy connections.
- Empty state: "No MCP connections configured. Configure MCP Proxy first." + link to `/settings/mcp-proxy`.

#### Danger zone (bottom of page)

Visually separated section per GitHub convention.

- **Uninstall this coding agent** — destructive confirm.

#### Override patterns (two distinct, by field type)

- **System prompts:** `Use default` toggle. When ON, textarea uneditable, runtime tracks shipped default. When OFF, textarea editable, DB value used.
- **Non-prompt fields (model, effort, MCP selection):** override-dot indicator next to the field label appears when value differs from the platform default; "Reset to default" link appears next to the dot.

#### Actions

- **Save changes** (top-right, primary, sticky).
- **Discard changes** (appears when dirty).
- **Per sub-agent: Remove** (destructive confirm).
- **Add sub-agent** (disabled at 8).
- **Uninstall coding agent** (Danger zone; destructive confirm).

#### States

| State | Pattern |
|---|---|
| Loading initial config | Skeleton sections preserving layout shape |
| Saving | Inline spinner on Save button; form disabled |
| Save success | Toast: "Changes saved." |
| Save error | Banner above form: "Couldn't save changes. Try again." |
| Validation error per field | Inline field error |
| Newly added (no prior config) | All fields show defaults; "Configured" status reached on first successful save |

#### Role-gated affordances

Settings pages are Admin-only for mutation. Builders landing here see the form in read-only state (fields disabled, no Save button, banner: "Only admins can change these settings"). All sections visible — transparency over hiding.

#### Removed from earlier drafts

- **Max turns** — not needed; backend manages.
- **Tools-as-editable** — premature flexibility; users haven't asked for tool-level control. Read-only display only.
- **Lessons context section** — backend-managed; max lessons + per-repo scoping happens server-side without user exposure.
- **Concurrency setting** — belongs on Workspace settings, not per-agent.
- **Custom workflow definition reference** — dropped; not a user-facing setting in M06.
- **Per-agent timeout** — covered by M05 workspace TTL (≤1h); no separate per-agent timer.

#### Details deferred to E2b (visual mocks)

- **System prompt textarea size + Maximize affordance.** Default expanded state: reasonably tall inline (~10–12 lines visible). Maximize icon expands to a much larger editor (modal or section-takeover); user can collapse back. Locks generous editing space for what are typically long prompts.
- Exact field widths, label placement, override-dot visual.
- Section header style.
- Danger zone visual treatment.

---

### E2a.3 — Dashboard

Surface: `/orgs/:slug/dashboard`. Bespoke page; most-touched landing for both roles.

Simplified design: one layout shared by both roles regardless of configuration state. A banner appears at the top if the org isn't configured; otherwise the page just shows operational state. The detailed setup work happens on the settings pages, not on Dashboard.

#### Page structure (top to bottom)

1. **Setup-required banner** (only when org not configured)
   - For Admin: "Setup is incomplete. Finish setup in settings." + link to `/orgs/:slug/settings`.
   - For Builder: "Setup is in progress. Ask [admin name] to finish setting up [org name]." + list of org Admins with email contact.
   - Builders cannot navigate to settings to fix this (Admin-only mutation), so the banner is informational + contact-oriented.

2. **Stat cards (4)** — at-a-glance operational summary.

   | Stat | Counts |
   |---|---|
   | **In flight** | Tickets currently active (Running + awaiting agent + awaiting human) |
   | **HITL pending** | Subset of in flight — tickets in `awaiting_human` state |
   | **Completed today** | Tickets reaching Done state today |
   | **Failed today** | Tickets reaching Failed state today |

   All four shown always (even at 0). Zero is informative.

3. **In flight band** — compact list of currently-active tickets.

   - 5–10 rows visible.
   - Per row: status icon, ticket title, repo, current stage, Builder avatar, time started.
   - "View all" link → `/orgs/:slug/tickets` filtered to in-flight states.
   - Empty state when no in-flight tickets: "No tickets in flight. Tickets appear here when a PR is opened."

4. **Needs attention band** — tickets currently in `awaiting_human` state.

   - 3–5 rows.
   - Per row: status icon, ticket title, repo, HITL prompt summary, Builder avatar.
   - Empty state when no HITL: "Nothing needs your attention right now."

#### Removed from earlier drafts

- **In-Dashboard setup checklist** — moved to the settings pages. Each settings sub-page shows its own configured state; Dashboard just points to settings via the banner. Simplifies Dashboard; treats incomplete setup as the edge case it is.
- **Recent activity band** — Notifications surface covers "what happened."
- **Open tickets stat card** — too overlapped with In flight; replaced with Failed today for genuine differentiation across the 4 cards.

#### States

| State | Effect |
|---|---|
| Org not configured | Setup-required banner present at top; stat cards show 0s; both lists show empty states. |
| Org configured, no tickets yet | No banner; stat cards show 0s; both lists show empty states. |
| Populated | No banner; stat cards show real counts; both lists show data. |
| Loading initial fetch | Skeleton stat cards + skeleton list rows. |
| Error fetching | Banner above the affected band ("Couldn't load. Retry."). |

#### Role-gated affordances

The Dashboard layout is the same for both roles. The setup-required banner is the only role-aware element (different copy).

#### Details deferred to E2b (visual mocks)

- Stat card visual (size, icon usage, color treatment for "Failed today" when >0).
- In flight + Needs attention list row visual.
- Banner visual treatment.
- Skeleton density.

---

### E2a.4 — Ticket detail

Surface: `/orgs/:slug/tickets/:ticketId`. The most complex page in the SPA. Bespoke. Where Builders spend the most time. Locks rendering of stage indicator, activity stream, findings, HITL panel, action affordances.

#### Header band

Single horizontal band at top of page. Contains:

- **Ticket title** (PR title for PR-review tickets; varies by future ticket types).
- **Status badge** (Running / awaiting HITL / Done / Failed / Cancelled).
- **Metadata strip** (small, inline): Builder avatar + name, repo, created time, source PR link (with GitHub icon, opens external).
- **Details disclosure** (collapsible): expands to show deeper metadata — ticket ID for copy-link, workflow execution ID, stage attempt count, additional timestamps.
- **State-aware action button** (top-right):
  - When in flight (Running / awaiting agent / awaiting human): **"Cancel [Stage name]"** (today: "Cancel Review") — destructive confirm.
  - When terminal (Done / Failed / Cancelled): **"Re-run [Stage name]"** (today: "Re-run Review") — cost-protective confirm.

No side rail. Header carries the essentials; Details disclosure carries the rest.

#### Stage indicator

Below the header. Compact horizontal sequence of stages for this ticket type.

- Today (single-stage tickets): just `Review`.
- Future multi-stage: e.g., `✓ Investigation → ▶ Fix → ⋯ Review` (completed / active / pending icons).
- Per stage shows: name, state, attempt number if >1 (e.g., "Review (Attempt 2)").
- Current stage visually highlighted.

#### HITL banner (only when HITL is active)

When the current stage is `awaiting_human`, a banner appears between the stage indicator and the main pane:

- Copy: "yaaos needs a decision before continuing."
- Action: scrolls to or focuses the HITL tab.
- Visually prominent (high-contrast, uses warning color from D2 tokens).

#### Main pane — three tabs

Tabs:

1. **Findings** — visible always. Default tab when ticket is terminal.
2. **Activity** — visible always. Default tab when ticket is in flight (no HITL pending).
3. **HITL** — visible only when ticket has had at least one HITL prompt (current or historical). Default tab when HITL is currently pending.

**State-aware default tab on page load:**
- HITL pending → HITL tab.
- In flight, no HITL pending → Activity tab.
- Terminal → Findings tab.

#### Tab: Findings

- List of findings. Per row:
  - Severity badge (low / medium / high).
  - Summary text (one-line).
  - File + line reference if applicable (clickable; opens external file in PR).
  - Status: open / acked / pushed-back.
- Click finding row → inline expand. Shows: rationale, suggested fix, code context. No modal.
- Per-finding actions: **Ack**, **Push back** (opens Teach-yaaos modal), **Teach yaaos** (opens Teach-yaaos modal with the finding as context).
- All actions available to all Builders (no Admin gating per A1 democratization).
- Empty state: "No findings yet." or "No findings — the review passed cleanly." depending on whether terminal.

#### Tab: Activity

Live SSE-streamed events from the running coding agent. Composes the Stream layout pattern.

- **Order**: chronological ascending (oldest at top, newest at bottom).
- **Auto-scroll**: page auto-scrolls to keep the newest event visible when ticket is in flight. User scrolling up disables auto-scroll for that session (sticky-scroll pattern).
- **Per event**: timestamp, event-type icon, event content.

  **Activity-event kind taxonomy (M06 baseline).** `ActivityEvent.kind` is plugin-defined and freeform; M06 ships icon mappings for the 6 kinds the `claude_code` plugin emits today (per `apps/backend/app/plugins/claude_code/service.py`). Unknown kinds fall back to a generic dot icon.

  | Kind | Icon (lucide) | Notes |
  |---|---|---|
  | `session_start` | `Play` | Agent session began. |
  | `subagent_dispatched` | `GitBranch` | Parent reviewer spawned a sub-agent. |
  | `tool_call_started` | `Wrench` | Bash/Edit/Read/etc. invocation began. `detail.name` carries the tool name. |
  | `tool_call_finished` | `CheckCircle2` / `XCircle` | Pick by `detail.exit_code === 0`. |
  | `assistant_message` | `MessageSquare` | Model emitted a user-facing message; `message` already pre-rendered. |
  | `result` | `Flag` | Run finished; `detail.status` carries the terminal outcome. |
  | *anything else* | `Circle` | Fallback for future kinds from new plugins. |
- **Collapse rule**: events with >3 lines collapsed by default. Latest event for in-flight tickets auto-expanded so live progress is visible without clicking.
- **Connection state indicators** (per C2's live-stream patterns):
  - Connecting: skeleton activity rows or small "Connecting…" indicator.
  - Connected, no events yet: empty-state component scoped to the pane.
  - Connected, events flowing: live render.
  - Disconnected: banner above the activity pane — "Activity stream disconnected. Retry." — does not block read access to already-streamed events.

#### Tab: HITL

Only present when at least one HITL has occurred on this ticket.

- **Current prompt panel** (when current stage is awaiting_human):
  - Prompt content (full, foregrounded).
  - Response interface — shape depends on prompt type (form / buttons / text input).
  - Submit response → calls backend; stage resumes; tab content updates.

**HITL prompt taxonomy (M06 baseline).** The workflow engine stores `question_payload` as an opaque dict per `PendingHumanDecisionRow` — there is no enforced schema today, and no production callers of `Outcome.hitl_pending()` yet. M06 SPA renders against a **discriminated-union schema** that backend HITL commands populate as they ship:

```
question_payload = {
  kind: "choice" | "text" | "form",       # discriminator
  title: str,                              # short headline shown in banner + tab
  body: str,                               # markdown description; can be multi-paragraph
  # kind === "choice":
  options?: [{value: str, label: str, variant?: "default"|"destructive"}],
  # kind === "text":
  placeholder?: str,
  multiline?: bool,
  # kind === "form":
  fields?: [{name: str, label: str, type: "text"|"textarea"|"select", options?, required?: bool}]
}
```

When `kind` is missing or unrecognized, the SPA renders a fallback: the raw `body` markdown plus a free-text response box. Backend commands written before M06 with raw payloads continue to work via the fallback.
- **History** (when there have been prior HITL exchanges):
  - Compact list of past prompts + responses.
  - Chronological.
- **Empty after resolution**: when the prompt has been responded to and the stage has resumed, the tab shows the history but no current prompt.

#### Live updates

The page subscribes to SSE for this ticket. Updates that trigger re-render:
- New activity events.
- Stage transitions.
- HITL prompts appearing / resolving.
- Findings being added.
- Ticket state transitions (running → done, etc.).

All updates land via TanStack Query invalidations driven by SSE events (per core/sse setup).

#### States

| State | Pattern |
|---|---|
| Loading initial fetch | Skeleton header + skeleton tabs |
| SSE connecting | Inline indicator in Activity tab |
| SSE disconnected | Banner above Activity pane |
| Cancel action | Destructive confirm modal |
| Re-run action | Cost-protective confirm modal with token-cost estimate |
| Finding ack / push-back | Optimistic? **No — pessimistic per C2.** Inline spinner until server confirms; toast on success |
| HITL submit | Inline spinner on submit button; tab updates on server confirm |

#### Role-gated affordances

Per A1's role consolidation: all Builders and Admins have the same affordances on Ticket detail. Same actions, same access, same UI.

#### Removed from earlier drafts

- **Side rail** — dropped; metadata in header + collapsible Details disclosure.
- **HITL as inline-only banner panel** — replaced with HITL-as-a-tab with prominent banner highlighting it when active.
- **Multi-attempt visibility on stage indicator** — kept (shows "Attempt N"), but no per-attempt navigation; current attempt is what shows.

#### Future multi-stage considerations (NOT M06, noted for context)

- The "Re-run [Stage name]" button today targets the current stage. Future multi-stage tickets need a "Re-run from scratch" alternative for cases where a downstream stage's failure is rooted in an earlier stage.
- The stage indicator already accommodates this visually; the action affordance gets a small dropdown when multi-stage tickets ship.

#### Details deferred to E2b (visual mocks)

- Stage indicator visual shape (pills / breadcrumb / progress bar).
- Activity event row visual (timestamp placement, icon vocabulary, expand affordance).
- Finding row visual (severity badge, inline expand interaction).
- HITL panel layout.
- Header band density (how much fits before Details disclosure is needed).
- Banner visual treatment.

---

### E2a.5 — Lessons list (Tier 2 derived)

Surface: `/orgs/:slug/lessons`. List archetype + card variant (rows are more spacious than Tickets list to accommodate the per-row content). Renamed from "Memory" per A3.

#### Per-row content

- **Title** — explicit lesson title (lessons have a dedicated title field; not derived from body).
- **Scope** — "All repos" label when scope is empty (org-wide); otherwise repo chips for the specific scoped repos.
- **Created by** — Builder avatar + name.
- **Created** — relative time; hover tooltip for absolute timestamp.

No body preview in the row. Click row → inline expand to show full lesson body + Edit / Delete actions.

#### Filters

- **Repo** — multi-select chips. Default "All repos."
- **Created by** — Builder filter.
- **Date range** — created-at.
- **Free-text search** — over title only (not body).

#### Sort

- Default: Most recently created descending.
- Alt: Alphabetical (by title).

#### Actions

- **Page-level: "Add lesson"** — modal per C1; available to all Builders.
- **Per row (in expanded state): Edit / Delete** — Builders edit/delete their own lessons; Admin edits/deletes any. Delete is destructive confirm.

#### States

- Empty: "No lessons yet. Lessons appear here when you teach yaaos something during a review."
- Empty (filtered): "Cannot find any lessons matching these filters."
- Loading: skeleton card rows.

#### Removed from earlier drafts

- Body preview in row (kept clean — title only).
- Applied count column.
- Most-applied sort option (no longer have the data column).

---

### E2a.6 — Notifications full page (Tier 2 derived)

Surface: `/notifications`. User-scoped, cross-org. Bespoke Stream layout per B2.

#### Per-row content

- **Read indicator** — subtle dot when unread.
- **Type icon** — lucide icon per notification type.
- **Source org** — org name (small).
- **Content** — one-line description.
- **Timestamp** — relative time.

Click row → navigate to the source ticket/page; auto-marks read.

#### Notification types (M06 initial set)

Three types. No "mention" or "assignment" (features don't exist yet).

1. **HITL waiting** — a ticket I triggered has a HITL prompt awaiting.
2. **Ticket completed** — a ticket I triggered finished.
3. **Ticket failed** — a ticket I triggered failed.

#### Fan-out rule

A notification's recipient is **exactly the ticket's builder** — the user identified by `Ticket.builder.user_id`. One notification per event; no broadcast.

- When `Ticket.builder.kind === "system"` (yaaos-triggered automated scan): no notification fires. Nothing to notify.
- When `Ticket.builder.kind === "user"` but `builder.user_id` is null (PR triggered by a GitHub user with no yaaos account): no notification fires. The GitHub PR comment serves as their feedback channel.
- Org admins do NOT get notified on Ticket failed by default in M06. Failure surveillance happens on the Dashboard's "Failed today" stat card. Admin-fan-out is a future-milestone concern.
- Cross-org: a user receives notifications for tickets in any org where they're a member — the Notifications surface is user-scoped (`/notifications`), not org-scoped.

#### Filters

- **Read state** — All / Unread / Read.
- **Org** — single-select; "All orgs" default.
- **Type** — multi-select.

#### Sort

Always chronological descending (newest first). Stream pattern, no alt sort.

#### Date grouping

Group rows by **Today / Yesterday / This week / Older**. Standard inbox pattern; helps "skim recent" use case.

#### Actions

- **Page-level: "Mark all read"** — affects current filter's items.
- **Per row: click → navigate + mark read.**

#### Pagination

Load more (consistent with Tickets list).

#### States

- Empty: "Nothing new. Notifications appear here when something needs your attention."
- Loading: skeleton rows.
- Disconnected (if SSE-backed): banner above, "Reconnecting…"

---

### E2a.7 — Notifications popover (Tier 2 derived)

Triggered from sidebar bell. Quick peek surface.

- Shows latest N unread notifications (cap ~10).
- Per row: same shape as the page, compact.
- Bottom: "Mark all read" + "See all" link → `/notifications`.
- Empty: "No new notifications."
- No filters, no pagination — peek-only.
- Closes on Escape or click-outside (Popover primitive behavior).

---

### E2a.8 — Settings — Auth (Tier 2 derived)

Surface: `/orgs/:slug/settings/auth`. Settings form archetype. Inherits from M03.

Sections: SSO setup (IdP metadata upload, SP metadata download, JIT toggle, exempt-Owner picker), Session timeout override (number + unit). Standard Save / Discard. Builders see read-only.

### E2a.9 — Settings — Members (Tier 2 derived)

Surface: `/orgs/:slug/settings/members`. List archetype with inline-edit.

Per row: avatar + display name, per-org handle, email, role badge (Admin / Builder), joined date, change-role dropdown, remove action (destructive confirm).

Page-level: "Invite member" — modal with email input + role picker.

Filters: Role (Admin / Builder / All), search by name/email/handle.

Builders see read-only; cannot invite, change roles, or remove.

### E2a.10 — Settings — Audit (Tier 2 derived)

Surface: `/orgs/:slug/settings/audit`. List archetype — dense forensic table.

Per row: timestamp (absolute), actor (avatar + name; "system" if no human), action (text key like `coding_agent.uninstalled`), target (entity affected). Click row → expand for full JSON payload detail.

Filters: date range, actor, action type (multi-select).

Sort: newest first; sortable by Timestamp / Actor / Action.

Pagination: Load more.

No edit affordances — audit is read-only by definition.

### E2a.11 — Settings — VCS (Tier 2 derived)

Surface: `/orgs/:slug/settings/vcs`. List archetype + picker-modal pattern (same shape as Coding Agents + MCP Proxy). Forward-compat for multi-VCS support (GitHub today; GitLab / Bitbucket / etc. future).

**List page:**

Per row: connection name (e.g., "GitHub"), status (Connected / Disconnected / Misconfigured), installation account, last verified, Edit (→ detail page) / Disconnect (destructive confirm).

Page-level: "Add VCS connection" — picker modal listing available VCS types (today: GitHub only) → confirm → navigate to detail page (`/settings/vcs/:provider`, e.g. `/settings/vcs/github`).

**Detail page (`/settings/vcs/:provider`):**

Settings form with:
- Connection-status card at top: install status, installation account, install / re-install link (external OAuth), disconnect action.
- Repo configuration section: list of connected repos; per row: name, last sync, remove action. "Add repo" — picker modal listing repos available from the installation.

Builders see read-only.

### E2a.12 — Settings — Workspace (Tier 3 derived; M05-new)

Surface: `/orgs/:slug/settings/workspace`. Settings form, single-section.

Fields: Provider (radio: in-memory / remote agent), Remote agent endpoint (URL — only when provider = remote agent), Authentication settings (only when provider = remote agent — **sigv4 config only**, per M05), Workspace TTL (display-only; M05 enforces ≤1h), Concurrency (number — workspaces in parallel for this org).

Builders see read-only.

### E2a.13 — Settings — MCP Proxy (Tier 3 derived)

Surface: `/orgs/:slug/settings/mcp-proxy`. List archetype + picker-modal pattern (like Coding Agents).

Per row: connection name (e.g., "Linear", "Notion"), status, last verified, Edit (→ detail page) / Disconnect (destructive confirm).

Page-level: "Add MCP connection" — picker modal → confirm → navigate to detail page (`/settings/mcp-proxy/:provider`).

Detail page: Settings form with OAuth status + per-provider config fields.

Builders see read-only.

### E2a.14 — Settings — API Keys (Tier 3 derived)

Surface: `/orgs/:slug/settings/api-keys`. Settings form, multi-field. Renamed from BYOK.

Fields (one per provider): masked input, last-4 visible, "Reveal" / "Replace" / "Remove" affordances.

M06 ships Anthropic only. Future providers (OpenAI, Google) follow the same field shape.

Same field also editable on each Coding Agent detail page; both surfaces hit the same backend API (per E2a.2 follow-up call).

Builders see read-only.

---

### E2a.15 — User — Details (Tier 3 derived)

Surface: `/user/details`. Settings form. Inherits from M03.

Sections:
1. **Profile** — display name (editable), avatar (initials in M06; upload deferred).
2. **Org handles** — per-org table: org name + handle in that org (inline edit).
3. **Emails** — primary email read-only; list of verified emails with primary marker. Add/verify/remove deferred unless trivially inherited from M02.
4. **GitHub handle** — `users.github_username` display; "Connect GitHub" / "Re-verify" button runs the one-shot OAuth verification flow per M03.

No role-gating. Users edit their own.

### E2a.16 — User — Security (Tier 3 derived)

Surface: `/user/security`. Settings form.

Sections:
1. **TOTP** — enrollment status, enroll / disable affordances, recovery codes when enrolled.
2. **Sessions** — "Sign out of all sessions" button (calls `POST /api/auth/logout-all`).

No role-gating.

### E2a.17 — User — Messaging (Tier 3 placeholder)

Surface: `/user/messaging`. Settings form. **Route in place; feature deferred.**

M06 placeholder content: page header + empty-state body — "Messaging settings appear here when Slack, Telegram, or Email notifications are configured for yaaos. Coming soon."

When messaging features ship (future milestone), this fills in with per-channel cards (Slack DM toggle, Telegram bot config, email preferences).

### E2a.18 — Login (Tier 3 bespoke)

Surface: `/login`. Bespoke auth shell. No sidebar; centered card layout.

yaaos does not manage its own user credentials — auth is exclusively through external providers (GitHub OAuth + org-configured SSO via SAML IdP). No password, no password reset, no email verification flow.

**Card content:**

1. yaaos logo / wordmark.
2. **Email input** — used for SSO discovery by email domain.
3. **Provider button** appears below the email input once an email is entered:
   - If domain matches a configured SSO IdP: "Continue with [IdP name]" button.
   - Else: "Continue with GitHub" button.
4. Click → external OAuth/SAML flow → return → 2FA challenge (TOTP if enrolled) → land on `/orgs` (picker) or `/orgs/:slug/dashboard` (single-org user).

**States:**
- Initial: just the email input + a "Continue with GitHub" affordance (the SSO option appears after typing email).
- Submitting / redirecting: inline spinner.
- Error after callback: banner + retry — "Couldn't sign in. Try again or use a different provider."

### E2a.19 — Org picker (Tier 3 bespoke)

Surface: `/orgs`. Sparse landing for multi-org users.

**Visibility rule:** appears at sign-in only when the user has >1 org. Single-org users auto-redirect to their dashboard. Zero-org users see this page with the empty state.

**Content:**

- Page header: "Choose an organization"
- Card list of orgs:
  - Each card: org name, role badge (Admin / Builder), last-used relative time.
  - Click → navigate to `/orgs/:slug/dashboard`.
- "Create new organization" affordance — modal per C1 (name + slug → create → redirect to new org dashboard).

**States:**

- Empty (zero orgs): "You're not in any organizations yet. Create one to get started." + Create button.
- Loading: skeleton cards.
- Multi-org user (default populated state).

---

## F1 — Implementation slicing

**Locked: phased on main.** No long-lived branches, no parallel `/v2/*` SPA.

### Rationale

- M06 is POC-phase; mid-flight visual inconsistency between phases is bounded and acceptable.
- Long-lived branch would rebase-hell against ongoing M05+ churn on `main`.
- Parallel UI behind a flag would double maintenance surface during M06; flag-flip is its own design problem.
- Phased catches per-surface regressions as they land rather than at the end.

### Phase ledger

Each phase ships to `main` independently. Phases sequential. Anchors before derived.

Status tags: `[done]` · `[partial]` · `[planned]`.

| # | Phase | Status | What lands |
|---|---|---|---|
| 1 | Token + primitive substrate | `[done]` | shadcn install + 22 primitives + Sonner; token reconciliation; axe-core wired in apps/e2e; design-tokens.md + components.md. |
| 2 | Layout + chrome composites | `[done]` | Sidebar + OrgSwitcher + NotificationsBell wired; layout composites (PageHeader / EmptyState / ErrorBanner / ConfirmModal / PickerModal / NotConfiguredBanner); route renames; member→builder rename + migration; org-scoping of tickets/lessons/reviewer routers; /api/orgs/mine + /api/orgs/config-status. Narrow-screen Settings flyout + per-page docs sweep are open polish items (D2.12). |
| 3 | Anchor — Tickets list | `[done]` | E2a.1 SPA + extended /api/tickets backend; M06 status vocab projection. Date-range + multi-builder filter UI deferred. |
| 4 | Anchor — Coding Agent detail | `[done]` | ClaudeCodeSettings schema extension (additive optional fields, D4.1); per-agent system-prompt overrides; BuilderReadOnlyBanner; Danger-zone uninstall ConfirmModal. Sticky Save/Discard footer + mcp_proxy_ids picker UI deferred to Phase 8 (paired with the MCP Proxy list page). |
| 5 | Anchor — Dashboard | `[done]` | E2a.3 SPA + GET /api/tickets/dashboard endpoint; NotConfiguredBanner placement. SSE invalidation is a polish item. |
| 6 | Anchor — Ticket detail | `[done]` | E2a.4 SPA + extended GET /api/tickets/:id + ack/push-back + HITL respond/history. Four standalone composites (StageIndicator, HitlPanel, FindingRow, ActivityEventRow). |
| 7 | Tier 2 derived + Notifications module | `[partial]` | Notifications backend module + endpoints + service tests + SPA bell + /notifications page all shipped. Lessons / Settings Auth+Members+Audit / VCS pages still on the legacy primitives. |
| 8 | Tier 3 derived | `[done]` | All 8 redesignable Tier-3 surfaces on shadcn: Login, Org picker, User Details / Security / Messaging, Settings VCS / API Keys / MCP Proxy. Settings Workspace page itself not built; M05 plumbing surfaces only via Coding Agents + Topbar workspace-connection banner. |
| 9 | Cleanup | `[partial]` | Legacy primitives (button/badge/card/dialog) + their barrel deleted. All chrome + page surfaces migrated to shadcn primitives. Yaaos-named transitional CSS tokens deleted from tailwind.config.ts + styles.css. Backend `/api/byok` + `/api/integrations` URL renames landed (A1, audit follow-up). Per-anchor axe-core specs added + green (A7). e2e suite green (7 passed, 2 pr-review skipped pending M05 outbox dispatcher). Open: FCP/LCP perf verification — pending a user-driven follow-up in a real-browser environment with Lighthouse. |

### Mid-flight visual inconsistency policy

- **Old surfaces work** between phases; they look unfinished but functional.
- **New surfaces don't reach back** to old components — once on the new design, a surface uses shadcn primitives + tokens consistently.
- **Chrome (sidebar + composites) ships in Phase 2** so the entire SPA visually agrees on chrome even when individual page content varies.

### Per-phase doc discipline

Per CLAUDE.md ("every code change updates docs in the same commit"), each phase updates:

- `apps/web/docs/<relevant>.md` for surfaces it touches.
- `apps/web/docs/patterns.md` when conventions change.
- `apps/web/docs/modularity.md` if the layer model changes (unlikely).
- New docs to create during the milestone: `apps/web/docs/design-tokens.md`, `apps/web/docs/components.md`.

### E2b sequencing relative to phases

Claude Design mocks for the four anchors should be produced **between Phase 2 and Phase 3** — after the substrate and chrome are in place (mocks can be drawn against real primitives), before any anchor implementation begins.

A mandatory checkpoint after the four mocks are accepted, before Phase 3 starts.

### Carry-forward into F2

F2's definition of done references these phases — "all 9 phases shipped" is one component of "done."

---

## F2 — Definition of done

Concrete, verifiable checklist for "M06 complete."

### A. All 9 phases shipped

- Phase 1 (substrate) merged.
- Phase 2 (chrome) merged.
- Phases 3–6 (anchors) merged; each with E2b mock + implementation matching it.
- Phases 7–8 (derived) merged; all 14 derived surfaces redesigned.
- Phase 9 (cleanup) merged.

### B. Every surface from A2 on the new design

- All 19 surfaces (5 bespoke + the rest under archetypes) implemented per E2a specs.
- No surface uses legacy hand-rolled primitives. `button.tsx`, `badge.tsx`, `card.tsx`, `dialog.tsx`, `placeholder-page.tsx` deleted from `shared/components/`.

### C. Token discipline verified

- `grep` shows zero arbitrary Tailwind values (`p-[7px]`, `bg-[#abc]`, etc.) in `apps/web/src/`.
- All colors trace to semantic tokens.
- All spacing traces to the locked spacing scale.

### D. Accessibility AA verified

- axe-core clean in CI for every page-level E2E test.
- Keyboard-only navigation pass completed for every surface.
- Every (foreground, background) semantic pair contrast-verified in both themes.
- Color-blindness simulator pass on Dashboard, Ticket detail, Notifications, Settings.
- `prefers-reduced-motion` honored on animated components.

### E. State patterns applied universally

- Every list has designed empty + loading + error states per C2.
- Every form has inline field-error pattern.
- Every save action produces a toast (or visible inline confirm) per D3.
- Live-stream states implemented on Ticket detail's activity pane.

### F. Voice + iconography consistent

- All copy follows the 12 voice rules from D3.
- No exclamation points, no emoji in core UI, no marketing-speak (`grep` verification).
- All icons drawn from `lucide-react` at the locked size rungs (16 / 20 / 24).
- All icon-only buttons carry `aria-label`.

### G. Docs updated

- `apps/web/docs/design-tokens.md` exists.
- `apps/web/docs/components.md` exists.
- `apps/web/docs/patterns.md` updated with state patterns, voice, density.
- Every `apps/web/docs/domain_*.md` updated for new surface content.
- `apps/web/docs/modularity.md` updated if structural changes landed.
- `docs/system-architecture.md` and `docs/setup.md` reviewed for stale references.
- `grep -rn "<old-thing>" apps/*/docs docs` returns zero matches for any renamed concept (`memory`→`lesson`, `integrations`→`mcp-proxy`, `byok`→`api-keys`, `account`→`user`, role names).

### H. CI clean

- `apps/web/bin/ci` green on every PR + `main`.
- `apps/e2e/bin/ci` green at milestone close.
- `web-security` RWX task green.

### I. No regressions on the existing flow

- Full PR-review E2E flow works end-to-end (webhook → ticket → workflow → workspace → coding agent → findings posted on PR).
- Hits all four anchor surfaces during the flow.
- Live SSE updates work on Ticket detail.

### J. Locked decisions match shipped code

- Sidebar layout matches B3.
- Two roles (Admin / Builder); `member` migrated to `builder`.
- Routes match B1's URL structure.
- Pattern A enforced — no `← Back` strings in the codebase.
- Pagination uses Load more — no infinite scroll, no numbered pagination.

### K. Backend changes scoped + documented

- Backend changes constrained to UI-driven REST additions/removals only.
- No new business logic, workflows, or entities introduced.
- Every backend touch documented in the milestone close-out.

### L. Browser support

- Tested on latest two versions of Chrome, Firefox, Safari, Edge.

### M. Performance targets

| Metric | Target |
|---|---|
| Cold-load FCP | < 1s |
| Warm-load FCP | < 500ms |
| Cold-load LCP | < 1.5s |
| Initial bundle size | < 200KB gzipped |

Achievable with focused effort: route-based code splitting, minimal HTML shell with critical CSS inlined, HTTP/2 / CDN delivery, lazy-loaded deps for non-initial routes.

LCP is the more meaningful metric for yaaos's data-heavy pages — measures when useful content (Tickets table, Ticket detail) is visible, not just "something is drawn."

---

## M06 planning — complete

All sections locked. Ready to write `START_HERE.md`, `PHASES.md`, and `DECISIONS.md` for autonomous execution.

E2b (Claude Design anchor mocks) deliberately deferred to the execution phase — they're produced between Phase 2 and Phase 3 of F1, after the substrate and chrome are in place so mocks reflect real primitives.
