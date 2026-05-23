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
