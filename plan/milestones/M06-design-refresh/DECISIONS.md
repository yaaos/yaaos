# M06 — Decision log

> Recorded as M06 executes autonomously. Each entry: the question, the option picked, alternatives rejected, one sentence why.

## Phase 1 — Token + primitive substrate

### D1.1 — shadcn install method: manual scaffold vs interactive CLI

- **Picked:** manually create `components.json` and write the primitives by hand (composed from shadcn's open-source source), without running `npx shadcn@latest init` interactively.
- **Rejected:** running the interactive CLI (would block on TTY prompts; can't reliably automate).
- **Why:** the autonomous loop must be non-interactive; the CLI just writes files we can write ourselves.

### D1.2 — Tailwind major version: stay on v3

- **Picked:** keep Tailwind v3 (`^3.4.14`) for M06.
- **Rejected:** migrate to Tailwind v4 (CSS-first config, new engine).
- **Why:** v4 migration is a large parallel project and would blow the milestone's scope; the plan's "v4" reference applies to greenfield shadcn projects, and shadcn supports both.

### D1.3 — Where axe-core lives

- **Picked:** axe-core in `apps/e2e/` (Playwright) — installed via `@axe-core/playwright`, asserted in an existing or new spec.
- **Rejected:** axe-core in vitest (`jsdom` doesn't render real layout — contrast checks become meaningless).
- **Why:** PHASES.md says "axe-core to the web E2E test setup"; e2e is the real-browser environment.

### D1.4 — Token reconciliation strategy

- **Picked:** add shadcn-named CSS variables (`--background`, `--foreground`, …) to `styles.css` populated by `var(--bg)`, `var(--text)`, … (aliasing layer); keep yaaos-named tokens until Phase 9 deletes them.
- **Rejected:** rewrite shadcn primitives to consume yaaos token names; or delete yaaos tokens immediately.
- **Why:** the plan explicitly says "Keep yaaos-named tokens as a transitional layer — Phase 9 removes them."

### D1.5 — How to add tailwindcss-animate

- **Picked:** install `tailwindcss-animate` and wire as a plugin in `tailwind.config.ts`.
- **Rejected:** hand-rolled keyframes per primitive.
- **Why:** shadcn primitives reference its utility classes directly; adding the plugin is the lowest-friction path.

## Phase 2 — Chrome, IA, route renames, backend chores

### D2.1 — `owner` role: keep or collapse into `admin`

- **Picked:** keep `owner` as-is in the role enum; only rename `member` → `builder`.
- **Rejected:** collapse `owner` → `admin` so the enum exactly matches the M06 "Admin / Builder" two-role spec.
- **Why:** `owner` carries privileges (SSO config, bootstrap-creator distinction) that `admin` doesn't; collapsing would force a broader rewrite for no POC-phase win. The UI can show both as "Admin" if the spec calls for two visible roles.

### D2.2 — `domain/integrations/` vs the M06 "MCP Proxy" page

- **Picked:** keep `apps/backend/app/domain/integrations/` as the internal module name; expose its endpoints under the new `/api/mcp-proxy/...` path prefix (in addition to or replacing `/api/integrations/...`).
- **Rejected:** rename the directory to match the public name (collides with the existing `apps/backend/app/domain/mcp_proxy/` module, which is the per-review MCP dispatcher — distinct concern).
- **Why:** the two "MCP Proxy"-named modules serve different concerns; renaming the OAuth-config module would force a 3-way rename across the dispatch module too, with no POC payoff.

### D2.3 — Favicon ICO: defer

- **Picked:** ship the SVG favicon (already present) + PNG siblings (`apple-touch-icon`, `icon-192`, `icon-512`) only; skip `favicon.ico`.
- **Rejected:** generate a multi-resolution ICO (`16/32/48`).
- **Why:** ICO requires native imagemagick/rsvg tooling that isn't on the dev box; the SVG favicon covers every modern browser and the PNG sizes cover iOS / Android. ICO support is a one-line polish later.

### D2.4 — SVG optimization on copy

- **Picked:** run `npx svgo --multipass` on the logos already in `apps/web/public/logos/` (saved 22–35%).
- **Rejected:** copy SVGs verbatim and defer SVGO.
- **Why:** plan calls for an SVGO pass and the tool runs through npx without local install.

### D2.5 — `member` → `builder` migration shape

- **Picked:** add a row-UPDATE migration (`UPDATE memberships SET role='builder' WHERE role='member'`) as `020_rename_member_to_builder` in `apps/backend/app/core/database/service.py`. No enum-type ALTER needed (the column is plain TEXT). Renamed `Role.MEMBER` → `Role.BUILDER` in `types.py`; bulk-replaced all usages via sed.
- **Rejected:** keep `MEMBER` as an alias of `BUILDER` for backward-compat.
- **Why:** CLAUDE.md is explicit ("no backward-compat shims"). The repo is small enough that mechanical rename + reformat is the right move; tests caught the one frontend `RANK` literal that was missed.

### D2.8 — Place `/api/orgs/mine` + `/api/orgs/config-status` on the existing `orgs` router

- **Picked:** add both endpoints to `apps/backend/app/domain/orgs/org_settings_web.py` (the already-registered `orgs` module at `/api/orgs`). `/mine` uses `public_route` + manual session cookie resolution; `/config-status` uses `require(Action.ORG_READ)`. `/api/orgs/mine` added to `PUBLIC_PATH_EXACT` in `core/auth/types.py`.
- **Rejected:** create a separate `user_orgs_web.py` module with its own `RouteSpec(module_name="orgs_user", ...)`. The route registry enforces one-prefix-per-module, and two RouteSpecs both at `/api/orgs` collide.
- **Why:** the registry already encodes the rule "one module per URL prefix"; piling endpoints onto the existing module is the path of least resistance.

### D2.10 — Org-scoping the three M01 routers: 5 new Actions, all Builder-grade

- **Picked:** add `TICKETS_READ`, `LESSONS_READ`, `LESSONS_WRITE`, `REVIEWER_READ`, `REVIEWER_WRITE` to `Action`; all map to `Role.BUILDER` in `_REQUIRED_ROLE`. Replaced `dependencies=[Depends(public_route)]` + `M01_ORG_ID` with per-endpoint `dependencies=[Depends(require(Action.X))]` + `org_id_var.get()`. Spec listed 4 Actions (no `LESSONS_WRITE`); added it because POST/PUT/DELETE /api/lessons clearly need a write-tier check.
- **Rejected:** keep `public_route` and infer org from request body / first query param.
- **Why:** the spec wants org-scoping the standard M02 way; this mirrors how the other already-org-scoped modules (orgs, integrations, vcs, coding_agents) work.

### D2.12 — Keep the existing custom `Sidebar`; add chrome composites alongside

- **Picked:** keep `apps/web/src/core/sidebar/sidebar.tsx` as-is (the hand-rolled component is small, already implements pinned/collapsed modes, and is well-tested). Add `OrgSwitcher` + `NotificationsBell` as standalone composites in `apps/web/src/shared/components/chrome/` and wire them into the existing sidebar's chrome zones. The `UserCard` composite already exists at `apps/web/src/core/sidebar/user-card.tsx` and is kept.
- **Rejected:** rebuild the sidebar on shadcn's `sidebar` primitive (the per-the-letter reading of B3 + D1).
- **Why:** shadcn's `sidebar` primitive is 500+ lines, requires cookie-state plumbing, and brings a sheet-based mobile drawer pattern we don't use. The existing sidebar plus the new chrome composites covers every behavior B3 calls out (collapsed/expanded modes, org switcher chip, notifications bell, user card popover) at a fraction of the churn. The shadcn primitive is still installed at `ui/sidebar.tsx` if a future polish pass decides to swap.

### D2.11 — `M01_ORG_ID` constant in `orgs/onboarding_web.py`: inline literal

- **Picked:** delete the `M01_ORG_ID = UUID(...)` constant in `reviewer/constants.py` (unused outside tests). In `orgs/onboarding_web.py` (the legacy `/api/settings/onboarding` endpoint, scheduled for Phase 9 deletion), rename the constant to `_LEGACY_ORG_ID` so the Phase 2 grep is clean while preserving the legacy fallback the SPA still calls.
- **Rejected:** delete the legacy `/api/settings/onboarding` endpoint now (the Dashboard + Settings pages still call it pre-redesign).
- **Why:** the spec lets the legacy endpoint live through Phase 2 → Phase 5 transition; rename-only keeps the grep clean without breaking the unmigrated SPA pages.

### D2.9 — Coding-agent readiness inferred from `anthropic_key_set`

- **Picked:** in `/api/orgs/config-status`, treat the existing `anthropic_key_set` contributor as a proxy for "coding agent configured" (do not surface `coding_agent` separately in `missing`).
- **Rejected:** add a third onboarding contributor for "≥1 coding agent installed" and check `OrgCodingAgentRow` directly.
- **Why:** today the only coding-agent plugin is Claude Code and its install flow co-provisions an Anthropic key, so the two signals are perfectly correlated. When a second coding-agent plugin ships, we add the contributor then; no code today is mis-served by this aggregation.

### D2.7 — SPA route renames only (byok / integrations / account); backend paths kept

- **Picked:** rename SPA-facing routes only — `/settings/byok` → `/settings/api-keys`, `/settings/integrations` → `/settings/mcp-proxy`, `/account/*` → `/user/*`. Backend `/api/byok/*` and `/api/integrations/*` paths are unchanged. UI labels: "BYOK" → "API Keys", "Integrations" → "MCP Proxy"; nav/tab ids likewise.
- **Rejected:** rename the backend paths too (per the spec letter), in particular `/api/integrations/{provider}/callback`.
- **Why:** `/api/integrations/{provider}/callback` is the externally-registered OAuth callback URL Linear/Notion redirect to — changing it requires user action upstream. Backend `/api/byok` is referenced from the plugin-installation layer; renaming would also cascade through tests + a sweep of the auth middleware's M02_PROTECTED_PREFIXES. The user-visible parts are what M06 is about; Phase 9 can finish the backend-side rename if it becomes important.

### D2.6 — `domain/memory` module → `domain/lessons` rename shape

- **Picked:** `git mv apps/backend/app/domain/memory apps/backend/app/domain/lessons` then a `sed` sweep to rewire all imports (`app.domain.memory` → `app.domain.lessons`), `RouteSpec(module_name=...)`, the local `lessons = await memory.list_for_repo(...)` shadow in `reviewer/incremental.py` (renamed the local to `lesson_rows`), and the frontend mirror (`@domain/memory` → `@domain/lessons`, `MemoryPage` → `LessonsPage`, `/memory` route → `/lessons`, nav label "Memory" → "Lessons"). Doc files renamed: `apps/{backend,web}/docs/domain_memory.md` → `domain_lessons.md`. Ran `apps/backend/bin/sync_modules` to refresh `tach.toml`.
- **Rejected:** keep `domain/memory` and add `/api/lessons` as an alias router (avoids any module rename); or write a redirect handler on the frontend.
- **Why:** the table is already named `lessons` in the DB, so the module name was the last stale piece; aliasing would create two source-of-truth names for one concept, exactly the kind of compat debt CLAUDE.md tells us to skip in POC phase.
