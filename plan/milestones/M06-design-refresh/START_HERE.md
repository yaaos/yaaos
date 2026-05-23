# M06 — Start here

> Entry point for autonomous execution of M06. If you're picking this milestone up cold, read this file end-to-end before touching code.

## What M06 is

Full design refresh of the yaaos SPA. Locked decisions: shadcn/ui as the primitive library, oklch tokens (already in `styles.css`), Tailwind, compact density, `member`→`builder` role rename, 4 redesigned anchor surfaces (Dashboard, Tickets list, Ticket detail, Coding Agent detail), plus 15 derived surfaces. Backend changes are scoped to UI-driven REST additions and renames — no new business logic.

## Read these files in this order

1. **[requirements.md](requirements.md)** — the spec. A1 → F2, all 18 sections locked. **The product source of truth.**
2. **[api-changes.md](api-changes.md)** — REST diff. What's new / renamed / extended / deleted, by surface and as a cross-cutting summary.
3. **[PHASES.md](PHASES.md)** — the 9-phase implementation order with per-phase definition of done. This is your task list.
4. **[design/anchors/](design/anchors/)** — Claude Design HTML mocks for the 4 Tier-1 anchors. Each anchor folder has a `yaaos/project/*.html` mock + a `prompt.md` showing what was asked. Use as visual reference during anchor implementation; not authoritative over requirements.md.
5. **[design/assets/logo/](design/assets/logo/)** — brand SVGs + PNG fallback. Copy to `apps/web/public/logos/` in Phase 2.

## Things to know before writing code

### Project context

- **yaaos is in POC phase** (per `CLAUDE.md`). Default to simpler implementations. No backward-compat shims when renaming internal symbols. **Don't dwell on edge cases**; pick a reasonable default and move on.
- **Two roles only**: `admin` and `builder`. The `member` role from M03 maps to `builder` in M06 — the rename cascades through the codebase. See api-changes.md § Cross-cutting concerns.
- **Three M01-era routers still use `M01_ORG_ID`**: tickets, memory, reviewer. Org-scoping them is a Phase 2 prerequisite, not a per-anchor task.

### Discipline (load-bearing — see `CLAUDE.md` for the full version)

- **TDD: Red → Green → Refactor.** Failing test first, every time.
- **Run `bin/ci`, not individual tools.** `apps/backend/bin/ci` if you touched backend; `apps/web/bin/ci` if you touched web; both if both. The Stop hook also runs them, but don't rely on the hook.
- **Run `apps/e2e/bin/ci` at the end of each phase that touches a user-visible flow.** Requires Docker stack — bring it up with `bin/dev-rebuild` first.
- **Every code change updates docs in the same commit.** Grep `apps/*/docs docs` for every symbol or concept you changed. The doc-link checker only catches broken links, not stale references — you are the gate.
- **No `TBD` / `TODO` / `coming soon` in `docs/`.** That belongs in `plan/` or a ticket.
- **Backend module changes need `apps/backend/bin/sync_modules`** — tach gets unhappy otherwise.

### Phase progression

- **Phases are sequential**, not parallel. The substrate (Phase 1) and chrome (Phase 2) gate everything; anchors (3–6) cascade their patterns into derived surfaces (7–8); cleanup (9) only makes sense once nothing is mid-flight.
- **Each phase ships to `main` independently.** Mid-flight visual inconsistency between phases is acceptable — F1 explicitly locked the "phased on main" approach over big-bang or behind-a-flag.
- **Checkpoint between Phase 6 and Phase 7.** After the 4 anchors land, hold for review before deriving Tier 2 surfaces from anchor patterns. If anchors aren't right, derivation amplifies the wrong choice.

### Decisions that may surprise you

A few things worth knowing up front so you don't re-litigate them mid-task:

- **No topbar.** All chrome lives in the sidebar. Org switcher chip is at the top of the sidebar, not in a topbar.
- **No breadcrumbs, no `← Back` links.** Sidebar context is the only back-affordance. If you find yourself reaching for breadcrumbs, the IA is wrong — surface it.
- **Pessimistic updates everywhere.** No optimistic flips in M06. UI waits for server confirm.
- **Pagination is "Load more".** No infinite scroll. No numbered pagination.
- **Drawers and wizards are cut.** Don't reach for them. If a flow seems to want one, see if a modal or a route push works.
- **The HITL feature is greenfield-on-greenfield.** The workflow engine supports it but no command actually returns `Outcome.hitl_pending()` today. M06 ships the SPA UI (Ticket detail HITL tab + Notifications) speculatively, with a fallback renderer for unknown prompt shapes. See requirements.md E2a.4 HITL prompt taxonomy + api-changes.md for the schema.

### Open questions that don't block Phase 1 or 2

Documented in api-changes.md § Open questions. The shorthand:

1. Do `hitl_pending` / `hitl_resolved` events fire from the workflow engine today? Verify before Phase 6.
2. Are TOTP recovery codes generated at enrollment today? If not, the `GET /api/account/totp/recovery-codes` endpoint is bigger than it looks — defer if necessary.
3. Source of `last_used_at` for org switcher — column vs sessions table. Lean column.
4. `findings_count` denormalization — column vs computed-on-read. Default computed.
5. `Lesson.created_by` source — column vs audit join. Default column.
6. OAuth callback rename strategy — redirect handler vs upstream re-config. Default redirect handler.
7. Notifications SSE filtering — server-side by `user_id`. (Likely. Confirm.)

## First PR

The first PR is **F1 Phase 1 — Token + primitive substrate**. Scope:

1. Init shadcn (`npx shadcn@latest init` in `apps/web/`). Choose Tailwind v4 / RSC: no / TypeScript.
2. Install the 18 primitives + Sonner listed in requirements.md D1.
3. Reconcile tokens: in `apps/web/src/styles.css`, alias shadcn's expected variable names (`--background`, `--foreground`, `--primary`, etc.) to oklch values. Keep the existing yaaos-named tokens as a transitional layer — they'll be removed in Phase 9.
4. Apply D4 baseline: focus-ring token, `prefers-reduced-motion` honored on Tailwind motion classes, contrast pairs verified (axe-core in CI; failures fail the build).
5. Update `apps/web/docs/` for the new token vocabulary — create `design-tokens.md` and `components.md`.

The Phase 1 PR does **not** touch chrome, IA, or routes. Those land in Phase 2.

## When you're stuck

- **Spec is ambiguous?** Treat requirements.md as authoritative; api-changes.md as supplementary. If neither resolves it, surface the question rather than guessing — silent assumptions become permanent decisions.
- **Code conflicts with the spec?** The spec wins; refactor the code. Unless the code was right and the spec is wrong, in which case update the spec first.
- **Hit a CLAUDE.md rule you're tempted to bend?** Don't. Surface it.
