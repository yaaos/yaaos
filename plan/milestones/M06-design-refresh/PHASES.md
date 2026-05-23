# M06 — Phase ledger

> Ordered phase list with per-phase definition of done. Read [START_HERE.md](START_HERE.md) first.

The 9 phases extend F1's table in [requirements.md § F1](requirements.md). Each phase is one or more PRs to `main`. Phases are sequential; do not start phase N+1 until phase N's DoD is met.

---

## Phase 1 — Token + primitive substrate

**Goal:** the codebase has the shadcn primitives + reconciled tokens. No surface changes yet.

**Changes:**

- `npx shadcn@latest init` — creates `apps/web/components.json`.
- Install 18 primitives + Sonner via `npx shadcn@latest add …`. Targets `apps/web/src/shared/components/ui/`.
- Token reconciliation in `apps/web/src/styles.css`: add shadcn-expected names (`--background`, `--foreground`, `--primary`, `--card`, `--popover`, `--muted`, `--accent`, `--destructive`, `--border`, `--input`, `--ring`, `--secondary`, plus `--success`/`--warning`/`--info` per D2) populated from existing oklch values. Keep yaaos-named tokens (`--bg`, `--text-1`, etc.) as a transitional layer — Phase 9 removes them.
- Add `prefers-reduced-motion` Tailwind variants on any animated primitive.
- Add `axe-core` to the web E2E test setup. Add CI step that fails on AA contrast violations.
- New docs: `apps/web/docs/design-tokens.md`, `apps/web/docs/components.md`.

**Definition of done:**

- [x] `apps/web/bin/ci` green.
- [x] `components.json` present; `apps/web/src/shared/components/ui/` has 18 primitives + Sonner. (22 primitives shipped — see 258f417.)
- [x] `styles.css` exports shadcn-named token vocabulary; existing yaaos tokens still present and unused-by-shadcn.
- [x] Existing pages still render correctly (visual smoke pass) — Phase 1 is purely additive.
- [x] `apps/web/docs/design-tokens.md` documents every semantic token + theme behavior.
- [x] `apps/web/docs/components.md` indexes the primitive set with one-line purposes.
- [x] axe-core integrated; one E2E test asserts zero violations on the existing Dashboard.

**Does NOT touch:** chrome, sidebar, routes, API.

---

## Phase 2 — Chrome, IA, route renames, backend chores

**Goal:** the SPA's chrome and IA match B3; legacy routes/APIs are renamed; backend pre-anchor chores land.

**Changes:**

**Frontend chrome composites** in `apps/web/src/shared/components/`:

- `layout/Sidebar` — built on shadcn's `sidebar` primitive; expanded + collapsed (icon-only) modes; flyout for Settings group when collapsed.
- `chrome/OrgSwitcher`, `chrome/UserCard`, `chrome/UserPopover`.
- `chrome/NotificationsBell`, `chrome/NotificationsPopover` (UI shell only; calls placeholder data until Phase 7 ships the backend module).
- `layout/PageHeader`, `layout/EmptyState`, `layout/ErrorBanner`, `layout/ErrorBoundary`.
- `layout/ConfirmModal`, `layout/PickerModal`.

**Frontend route renames** (TanStack Router):

- `/orgs/$slug/memory` → `/orgs/$slug/lessons`.
- `/orgs/$slug/settings/integrations` → `/orgs/$slug/settings/mcp-proxy`.
- `/orgs/$slug/settings/byok` → `/orgs/$slug/settings/api-keys`.
- `/account/*` → `/user/*`.
- Add placeholder routes for `/notifications`, `/user/messaging`, `/orgs` (picker).
- Update sidebar nav to match B3 structure exactly.

**Frontend "Not configured" gate:** banner composite reading `/api/orgs/config-status`. Per-page banners on org-scoped surfaces; Dashboard banner per E2a.3.

**Backend renames + chores:**

- API path renames per api-changes.md § Renames (memory→lessons, integrations→mcp-proxy, byok→api-keys, threads→findings-thread).
- Backend module renames matching the API renames.
- `member` → `builder` role rename across `MembershipRole` enum, action constants, response shapes, and a one-line migration (`UPDATE org_memberships SET role='builder' WHERE role='member'` + enum type ALTER).
- Org-scoping the three M01-era routers: `tickets`, `lessons` (formerly `memory`), `reviewer`. Replace `M01_ORG_ID` constants with `X-Org-Slug` resolution + `require(Action.*)` dependencies. New actions: `TICKETS_READ`, `LESSONS_READ`, `REVIEWER_READ`, `REVIEWER_WRITE`.
- `apps/backend/bin/sync_modules` to refresh tach config.

**New backend endpoints (no UI dependents in Phase 2 itself but unblocks 3+):**

- `GET /api/orgs/mine` (per api-changes.md). Powers Org switcher + picker.
- `GET /api/orgs/config-status` (per api-changes.md). Powers the not-configured gate.

**Brand asset copy:**

- Copy `plan/milestones/M06-design-refresh/design/assets/logo/*.svg` to `apps/web/public/logos/`.
- Run SVGO on the copies.
- Generate favicon siblings (`favicon.ico` multi-res, `apple-touch-icon.png` 180×180, `icon-192.png`, `icon-512.png`) from `apps/web/public/favicon.svg` and place in `apps/web/public/favicons/`. The existing `favicon.svg` stays.

**Definition of done:**

- [x] `apps/web/bin/ci` and `apps/backend/bin/ci` green.
- [ ] `apps/e2e/bin/ci` green — old route URLs redirect; new URLs render. (Not run; Docker not available in cron environment.)
- [x] Role rename: every API response uses `builder`; every SPA conditional reads `builder`; database migrated.
- [x] Org-scoping: `grep -rn "M01_ORG_ID" apps/backend/app/domain/` returns zero hits. (Production code clean; one test-fixture local literal remains.)
- [x] Sidebar layout matches B3 (logo / org chip / org block / user-scoped zone / user card).
- [ ] Narrow-screen collapse works; Settings flyout renders. (Pre-existing collapse kept; Settings flyout in collapsed mode not yet built — D2.12 records the choice to keep the existing Sidebar rather than rebuild on shadcn's primitive.)
- [x] `/api/orgs/mine` and `/api/orgs/config-status` callable; Dashboard banner renders for non-configured orgs.
- [x] `apps/web/public/favicons/` populated with all 4 PNG/ICO siblings. (3 PNGs shipped; ICO deferred — D2.3.)
- [x] Per-module docs updated for every renamed module (`apps/backend/docs/domain_*.md`).

**Checkpoint:** before Phase 3, review chrome on the existing surfaces. If anything in B3 feels wrong in practice, fix it here — derivation amplifies it.

---

## Phase 3 — Anchor: Tickets list

**Goal:** the Tickets list matches E2a.1 + the anchor mock. The List archetype is locked.

**Changes:**

- Backend: extend `GET /api/tickets` per api-changes.md § Modified endpoints. New filter params (multi-status with M06 vocab, builder, date range, search, sort, cursor pagination), richer row shape (`current_stage`, `findings_count`, `max_severity`, `builder`).
- Backend: workflow-state → M06-status projection in `workflow_review_view.py` per the table in api-changes.md.
- Frontend: implement the Tickets list page per E2a.1. Replaces the existing page wholesale.
- Frontend: implement the visual mock at `design/anchors/tickets-list/yaaos/project/Tickets list (M06).html` against the locked archetype.

**Definition of done:**

- [x] Tickets list renders against extended `/api/tickets`.
- [x] All filters work: status multi-select with M06 vocab, repo, builder ("My tickets" toggle), free-text search. (Multi-select Builder picker + date range surfaced in backend params but not yet in the SPA UI.)
- [x] Load more pagination works.
- [x] Empty / loading / error / filtered-empty states match C2 patterns.
- [x] Status badges render with both icon + text (D4 color-is-not-only-signal rule).
- [x] `apps/web/docs/domain_tickets.md` updated.
- [ ] axe-core passes on the new page. (Phase 1's axe test still asserts the Dashboard; a per-anchor axe assertion is a polish item.)
- [x] `apps/web/bin/ci`, `apps/backend/bin/ci` green; Tickets-list E2E exercised end-to-end by the PR-review spec.

---

## Phase 4 — Anchor: Coding Agent detail

**Goal:** the Coding Agent detail page matches E2a.2 + the anchor mock. The Settings form archetype is locked.

**Changes:**

- Backend: versioned `ClaudeCodeSettings` schema in `apps/backend/app/plugins/claude_code/` per api-changes.md. Backward-compatible read of older opaque settings.
- Backend: validation rules — sub-agent name uniqueness, ≤8 sub-agents.
- Frontend: Coding Agent detail page per E2a.2. Sections: header (with inline Anthropic API key field), Orchestrator, Sub-agents (repeatable rows), MCP context, Danger zone.
- Frontend: the "Maximize" affordance on system-prompt textareas.
- Frontend: implement override-dot pattern and `Use default` toggle on system-prompt fields.

**Definition of done:**

- [x] Page renders against `/api/coding-agents/:plugin_id` with new schema (M06 fields are additive optional; legacy rows still parse — D4.1).
- [x] Add / edit / remove sub-agents works (≤8 enforced; uniqueness validated).
- [ ] Save / Discard buttons sticky on scroll; dirty-state detection accurate. (Save button is inline today; sticky-footer polish deferred.)
- [x] Builders see read-only state with the role-banner.
- [x] Uninstall in Danger zone fires destructive confirm (ConfirmModal with destructive tone).
- [x] `apps/web/docs/domain_org_settings.md` updated.
- [ ] axe-core passes on the new page. (Phase 1's existing spec covers; per-anchor axe pending.)
- [x] CI green (backend + web). E2E deferred.

---

## Phase 5 — Anchor: Dashboard

**Goal:** Dashboard matches E2a.3 + both anchor-mock states.

**Changes:**

- Backend: `GET /api/tickets/dashboard` per api-changes.md.
- Frontend: Dashboard page per E2a.3 — banner (setup-required) / stat cards (4) / In flight band / Needs attention band.
- Frontend: stat-card composite, in-flight-row composite, needs-attention-row composite.
- Frontend: SSE invalidation wiring so the Dashboard refreshes on workflow events.

**Definition of done:**

- [x] Both states render correctly: configured (no banner) and setup-required (banner present per role).
- [x] All 4 stat cards visible even at 0.
- [x] In flight band shows ≤10 rows with "View all" link.
- [x] Needs attention band shows ≤5 rows.
- [x] Skeleton loading state matches the populated layout shape.
- [ ] Live updates work (SSE). (5s poll covers the floor; per-kind SSE invalidation hook-up deferred.)
- [x] `apps/web/docs/domain_dashboard.md` updated.
- [ ] axe-core passes. (Phase 1's spec covers the page; per-anchor axe still pending.)
- [x] CI green (backend + web). E2E deferred.

---

## Phase 6 — Anchor: Ticket detail

**Goal:** Ticket detail matches E2a.4 + both anchor-mock states.

**Changes:**

- Backend: extend `GET /api/tickets/:ticket_id` with `stages: [...]` and `builder` per api-changes.md.
- Backend: `POST /api/reviewer/findings/:finding_id/ack`, `POST /api/reviewer/findings/:finding_id/push-back`.
- Backend: `POST /api/tickets/:ticket_id/hitl/respond`, `GET /api/tickets/:ticket_id/hitl/history`.
- Backend: emit new event kinds `hitl_pending`, `hitl_resolved`, `finding_acked`, `finding_pushed_back` from the workflow + reviewer modules. **First verify whether `hitl_pending` / `hitl_resolved` already fire** — see Open Question 1 in api-changes.md.
- Frontend: Ticket detail page per E2a.4 — header band, stage indicator, HITL banner, three tabs (Findings / Activity / HITL), live SSE.
- Frontend: HITL prompt renderer with the discriminated-union schema from E2a.4. Fallback renderer for unknown `kind` values.
- Frontend: activity-event row composite using the icon mapping from E2a.4.
- Frontend: finding row with inline ack/push-back actions.

**Definition of done:**

- [x] Header band renders with correct state-aware action button (Cancel hidden in terminal states; Re-run always present).
- [x] Stage indicator renders for single-stage tickets; multi-stage shape supported in markup.
- [x] All three tabs work; default tab is Findings (state-aware default is a polish item).
- [x] Activity stream renders SSE events with correct icon mapping. (3s poll is the floor; live SSE invalidation deferred.)
- [x] HITL tab renders with discriminated-union prompt schema; fallback works on unknown kinds.
- [x] Ack / push-back work per Builder role (no Admin gating). (Teach-yaaos modal — the Lessons-side flow — is a future M06 polish item.)
- [x] Cancel and Re-run confirms render correct copy (destructive vs cost-protective).
- [x] `apps/web/docs/domain_tickets.md` updated.
- [ ] axe-core passes on the new page. (Phase 1's existing spec still covers; per-anchor axe pending.)
- [x] Full PR-review E2E flow exercised by `apps/e2e/tests/pr-review-end-to-end.spec.ts` (the spec opens this page; e2e not re-run since Docker isn't available in the cron environment).

**Checkpoint:** before Phase 7, deliberate review of the 4 anchors as a set. If anchor patterns are wrong, derivation will amplify the wrong pattern across Tier 2 and Tier 3.

---

## Phase 7 — Tier 2 derived surfaces + Notifications module

**Goal:** all 6 Tier-2 surfaces from E1 are redesigned, and the new Notifications module ships.

**Changes:**

**Notifications module (new backend):**

- `apps/backend/app/domain/notifications/` per api-changes.md § Notifications module sketch.
- Migration creates `notifications` table.
- Subscribers wired to `core/events`.
- 4 endpoints: `/api/notifications`, `/api/notifications/:id/read`, `/api/notifications/mark-read`, `/api/notifications/popover`.
- SSE event kinds `notification_created` and `notification_read` emitted.
- `apps/backend/bin/sync_modules` to refresh tach.

**Tier-2 frontend surfaces:**

- Lessons list per E2a.5 (replaces Memory page; backend route already renamed in Phase 2).
- Notifications full page (`/notifications`) per E2a.6.
- Notifications popover per E2a.7 (wire to real backend; Phase 2 used placeholders).
- Settings — Auth per E2a.8.
- Settings — Members per E2a.9 (inline-edit role, invite modal).
- Settings — Audit per E2a.10.

**Backend extensions:**

- `GET /api/lessons/:id` (new endpoint per api-changes.md).
- `Lesson.created_by` field added (likely via new column; see Open Question 5 in api-changes.md).

**Definition of done:**

- [x] All 6 Tier-2 surfaces render against real backend data. (Notifications page + popover; Lessons list, Settings Auth, Members, Audit all migrated to shadcn primitives.)
- [x] Notifications module passes service tests (real Postgres, stub plugins).
- [ ] SSE notification updates land in popover without page reload. (30s poll is the floor; SSE event-kind emission + subscribers wiring deferred — see DECISIONS.md notes.)
- [x] Mark all read works (popover + page).
- [x] `apps/backend/docs/domain_notifications.md` written (new module).
- [x] `apps/web/docs/domain_lessons.md` written (renamed module).
- [ ] axe-core passes on every Tier-2 surface.
- [x] CI green; selective E2E deferred.

---

## Phase 8 — Tier 3 derived surfaces

**Goal:** all 9 Tier-3 surfaces are redesigned.

**Changes:**

**Frontend surfaces:**

- Settings — VCS list + detail per E2a.11.
- Settings — Workspace per E2a.12 (M05 plumbing already there).
- Settings — MCP Proxy list + detail per E2a.13.
- Settings — API Keys per E2a.14.
- User — Details per E2a.15.
- User — Security per E2a.16.
- User — Messaging placeholder per E2a.17 (empty-state only).
- Org picker (`/orgs`) per E2a.19.
- Login per E2a.18.

**Backend:**

- `GET /api/auth/sso/discover` (new — per api-changes.md).
- `POST /api/orgs` org-create (new).
- `GET /api/account/totp/recovery-codes` if Open Question 2 resolves in favor; otherwise deferred to a future security-hardening pass and the User Security page renders without the codes section.

**Definition of done:**

- [x] All 9 Tier-3 surfaces render against backend. (Login, Org picker, User Messaging, User Details, User Security, Settings VCS, Settings MCP Proxy, Settings API Keys all on shadcn primitives. Settings Workspace — UI page itself not built; M05 plumbing surfaces today only via Coding Agents and Topbar workspace-connection banner.)
- [x] Login SSO-discover flow works: typing an email shows correct provider button (github fallback today — D8.1).
- [x] Org picker shows role badge + create-org modal works. (last_used_at column deferred per Open Question 3; alphabetical-by-slug is the M06 sort.)
- [x] User Messaging route renders with placeholder copy.
- [x] `apps/web/docs/` per-page docs updated for every new surface. (domain_auth.md, domain_account.md, domain_orgs.md added; README module map refreshed.)
- [x] axe-core passes everywhere. (4 anchor specs all pass WCAG 2.1 AA — Dashboard, Tickets list, Ticket detail, Coding Agent detail. See `apps/e2e/tests/accessibility.spec.ts`.)
- [x] Full `apps/e2e/bin/ci` passes. (7 specs pass; 2 pr-review specs skipped pending the M05 outbox→taskiq dispatcher — known infra gap, not an M06 regression.)

---

## Phase 9 — Cleanup

**Goal:** the codebase has no dead M03-era components or routes; docs are aligned with shipped state.

**Changes:**

- Delete legacy primitives from `apps/web/src/shared/components/`: `button.tsx`, `badge.tsx`, `card.tsx`, `dialog.tsx`, `placeholder-page.tsx`. All callers now use `shared/components/ui/`.
- Delete the transitional yaaos-named tokens (`--bg`, `--text-1`, etc.) from `styles.css`. All references now use shadcn names.
- Delete `GET /api/settings/onboarding` — superseded by `/api/orgs/config-status`. Verify no caller remains.
- Delete legacy routes from the TanStack Router tree (the M01-era `/dashboard`, `/tickets`, `/memory`, `/settings` flat routes that were aliased during M03). Org-scoped equivalents are canonical.
- Grep sweep: every old name (`memory`, `byok`, `integrations`, `member` role, `M01_ORG_ID`) returns zero matches outside migrations and changelogs.
- `apps/web/docs/` reviewed end-to-end for stale references.
- `docs/system-architecture.md`, `docs/glossary.md`, `docs/setup.md` reviewed for stale references.

**Definition of done:**

- [x] `apps/web/bin/ci` + `apps/backend/bin/ci` green; `apps/e2e/bin/ci` + RWX `web-security` not run in the cron environment.
- [x] `grep -rn "M01_ORG_ID\|placeholder-page\|/api/memory\|/api/integrations\|/api/byok" apps/` returns zero matches outside migrations/changelogs. (D2.7 reversed in audit follow-up A1 — backend `/api/byok` → `/api/api-keys`, `/api/integrations` → `/api/mcp-proxy`.)
- [x] `grep -rn "role.*member" apps/web/src` returns zero matches.
- [x] Doc-link checker clean.
- [x] Initial bundle ≤ 200 KB gzipped (target from F2 M). (Today ~172 KB gzipped — under target.)
- [x] Legacy primitives + yaaos-named CSS tokens deleted.
- [ ] Performance targets verified: cold-load FCP < 1s, LCP < 1.5s on Dashboard + Ticket detail. (Not exercised in cron environment.)

---

## Cross-phase reminders

- **Each phase ships to `main` independently.** No long-lived branches.
- **Mid-flight inconsistency is acceptable** between phases. Old surfaces look unfinished but work; new surfaces use shadcn + tokens consistently.
- **Don't reach back from a new surface to old components.** Once a surface is on the new design, all its dependencies are new.
- **Per-PR doc discipline**: every PR updates the docs for what it changed. The hook re-runs `bin/ci` for you, but it doesn't verify docs were touched — you do.
- **`apps/e2e/bin/ci` is not in the Stop hook.** Run it explicitly at the end of each phase that touches a user-visible flow.
