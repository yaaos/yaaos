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
