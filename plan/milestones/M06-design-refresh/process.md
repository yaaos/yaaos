# M06 design process — the meta plan

> The order in which we will think through the design of yaaos. Each section is a working session between Jack + Claude. Output of each session is a locked block that gets persisted into `requirements.md` (or `architecture.md` / a style-guide doc as appropriate). Sections are sequenced so that earlier decisions feed later ones.

## How to read this

- **Coaching note.** Jack is a backend / devops engineer learning design vocabulary. This doc uses real design + React terms and defines them inline the first time they appear. When a term shows up later without a definition, it was defined earlier in this file.
- **One section per working session.** We don't rush. The output of each is a small, locked set of decisions.
- **Claude Design** (Anthropic's design tool — generates visual mockups from prompts) has a specific role: anchor visuals after IA + flows are decided. Where it fits is called out in each section's *Claude Design fit* line.
- **Status legend.** `[ ] todo` · `[~] in progress` · `[x] locked`.

---

## Phase A — Foundation: who, what, why

The "who is this for and what are they doing" layer. Skipping this is how products end up beautifully designed for the wrong audience.

### A1. Users + jobs-to-be-done `[ ]`

- **What it is.** A short list of the user types that touch yaaos and the *jobs* each comes to do. "Jobs-to-be-done" (JTBD) is design-speak for "the outcome a user wants" — phrased as a goal, not a feature. Example: not "use the dashboard," but "find out quickly whether anything's stuck."
- **Why first.** Every IA and flow decision later traces back to a job. If "find what's stuck" is a primary job, then surfacing in-flight + failed work prominently is non-negotiable.
- **What we produce.** 2–4 user types (e.g. "admin who configures the org," "developer who opened the PR being reviewed," "manager checking budget burn"). For each: 3–6 jobs in priority order.
- **Claude Design fit.** None. This is words, not pixels.

### A2. Surface inventory `[ ]`

- **What it is.** A flat list of every page, view, panel, and modal that exists in the SPA today, plus any that M05 adds. No grouping yet — just the inventory.
- **Why now.** We can't design IA without knowing the full set of surfaces. Grouping in the wrong order leads to "where the hell is X?" outcomes.
- **What we produce.** A table. Columns: surface name, current route, current state (exists / M05-added / proposed-new), and a one-liner on what's on it.
- **Claude Design fit.** None.

### A3. Mental model `[ ]`

- **What it is.** The conceptual hierarchy users hold in their head when they use yaaos. Probably something like `Org → Repo → Ticket → Workflow → Findings`. The point isn't to invent it — it's to write down what the product's nouns *actually are* and how they nest.
- **Why now.** The mental model drives the URL structure, breadcrumb shape, and what's "inside" what in the sidebar. If `Workflow` lives mentally inside `Ticket`, then `/tickets/123/workflow` is right and a top-level `/workflows` is confusing.
- **What we produce.** A short noun list with parent/child relationships. Plus a glossary cross-check against `docs/glossary.md` (so the UI uses the same words as the backend).
- **Claude Design fit.** None.

---

## Phase B — Structure: how it's organized

Now we group the inventory into a coherent map of the product.

### B1. Information architecture (IA) `[ ]`

- **What it is.** "Information architecture" = how content + surfaces are *grouped and labeled* so users can find them. The classic deliverable is a sitemap: top-level sections, what lives under each, and the labels (the human-readable names) for each.
- **Why now.** Everything below depends on it. Sidebar shape, URL structure, breadcrumbs, even what counts as "a page" — all fall out of IA.
- **What we produce.** A sitemap. Top-level groups (likely something like Dashboard / Tickets / Memory / Settings, but we'll re-decide from scratch), what nests under each, and the labels for each. Plus a routing map (`/dashboard`, `/tickets`, `/tickets/:id`, `/settings/coding-agents`, etc.).
- **Decisions to make.** Org-scoped vs user-scoped split — keep current model or evolve? Does "Activity" or "Workflows" need its own top-level home, or stays inside Tickets? Are Workspaces a viewable thing for admins or strictly invisible?
- **Claude Design fit.** Light. Could sketch a sidebar mock from the locked sitemap, but the IA itself is decided in prose.

### B2. Page archetypes `[ ]`

- **What it is.** An "archetype" is a *kind of page* — a reusable layout template. Most apps have 5–8 archetypes: list view, detail view, dashboard, settings form, stream/feed, wizard, empty state, full-screen takeover. Locking these means every list view in yaaos looks the same — same filter bar position, same header shape, same row density. Consistency comes from archetypes, not from copy-pasting one page to another.
- **Why now.** Once IA is locked, we know which surface needs which archetype. We define archetypes once and reuse.
- **What we produce.** 5–8 archetypes named, each with a canonical layout (sketched in ASCII or described in prose). Map every surface from A2 to one archetype.
- **Term: layout.** "Layout" in React-land is the persistent shell — sidebar + topbar + outlet — that wraps every page. In TanStack Router this is a route layout component. Archetypes live *inside* the layout's outlet.
- **Claude Design fit.** **Strong.** This is exactly what Claude Design is good for — produce one anchor mock per archetype. These mocks become the visual reference for everything in the SPA.

### B3. Navigation model `[ ]`

- **What it is.** How users move between surfaces. Sub-questions: sidebar always visible or collapsible? Tabs vs sub-routes for splitting a detail page? Breadcrumbs yes/no? Back button behavior? Browser back vs in-app back?
- **Why now.** Affects every page's header. Determines whether modals "navigate" (push to history) or "overlay" (don't).
- **What we produce.** A small ruleset. Examples: "sidebar is always visible on desktop ≥1024px, collapses to icon-only below;" "every detail page has breadcrumbs in the header;" "modals do not change the URL; drawers do."
- **Claude Design fit.** Light. The header shape from B2's archetypes already encodes most of this.

---

## Phase C — Behavior: how a user moves

Structure tells us where things live. Behavior tells us what happens when a user does something.

### C1. Standard UX flows `[ ]`

- **What it is.** The locked answer to "for action X, do we use a modal, a new page, a drawer, an inline form, or a toast?" Lock these once, use them everywhere — otherwise the app feels random.
- **Terms.**
  - **Modal** — overlay centered on screen, blocks the background. Best for short, decisive actions (confirm delete; quick add). Bad for long forms.
  - **Drawer / side panel** — slides in from the edge, background often dimmed. Best for "show me more about this thing" without losing the list behind it (e.g. preview a ticket from the list).
  - **Full page / route push** — navigate to a new URL. Best for long forms and anything you'd want to deep-link or back-button to.
  - **Inline edit** — edit in place on the current view. Best for one-field changes (rename, toggle).
  - **Toast** — transient notification at the corner. Best for "save succeeded" feedback. Never for anything the user has to read carefully.
  - **Popover** — small floating panel anchored to a trigger. Best for menus, simple pickers.
- **Why now.** The next time you ask "how do I add a coding-agent plugin?" we need one answer, not five.
- **What we produce.** A flow table. Rows: each common action (open ticket, add plugin, edit setting, confirm destructive action, view long log, switch org, etc.). Columns: pattern used + why.
- **Claude Design fit.** Light. Once locked, Claude Design can sketch the actual drawer/modal/page mock as part of B2's archetype set.

### C2. State patterns: empty, loading, error, success `[ ]`

- **What it is.** Every list, every panel, every chart has four states beyond the "happy populated" view: empty, loading, error, partial. Lock one designed pattern per state, reuse across surfaces.
- **Why now.** Skipping this is how apps end up with raw spinners next to nicely-designed cards next to red-text error messages. Inconsistent state = looks unfinished.
- **What we produce.** One spec per state. Empty: icon + headline + one-line explanation + primary action. Loading: skeleton (gray block placeholders matching the final shape) or spinner — pick per archetype. Error: icon + what failed + retry action. Success: usually a toast, sometimes inline.
- **Term: skeleton.** Placeholder blocks shaped like the content that will load. Better than spinners because the layout doesn't jump when content arrives.
- **Claude Design fit.** **Medium.** Worth sketching the empty state for a populated archetype (e.g. the Tickets list when there are no tickets). Loading + error are usually obvious from there.

### C3. Information density `[ ]`

- **What it is.** How much info per square inch. Devops tools tend toward dense (think Linear, Datadog, Grafana). Marketing tools tend toward airy (think Stripe dashboard, Notion). yaaos is closer to Linear — but we need to decide *how* dense.
- **Why now.** Affects the type scale (smaller text = more density), spacing scale, table row height, padding everywhere. Wrong call here and the redesign feels off without you being able to say why.
- **What we produce.** A density target (compact / comfortable / spacious) and the rules that come with it: base font size, base row height, base padding. Also: do we ship a user-toggle, or lock one density?
- **Claude Design fit.** Light. Easier to feel by mocking one list at two densities and comparing.

---

## Phase D — Visual language: how it looks

We have structure and behavior. Now the look.

### D1. Component library decision `[ ]`

- **What it is.** The library of pre-built React primitives we build on. Current state: hand-rolled, 4 primitives. Time to switch.
- **Options for Tailwind + React.**
  - **shadcn/ui** — recipe-based: a CLI copies component code into your repo. Built on Radix primitives (correct accessibility + keyboard behavior). Tailwind-native. Most popular pick today. You own the code, so customization is real edits, not theme-overrides.
  - **Radix UI primitives** — the unstyled, accessible primitives that shadcn/ui wraps. You'd style them yourself. More work, more flexibility.
  - **Headless UI** — Tailwind Labs' own headless library. Smaller scope than Radix.
  - **Ark UI / Park UI** — newer, similar to Radix.
  - **Mantine / Chakra / MUI** — full styled libraries with their own theming systems. Not idiomatic with Tailwind (they fight rather than compose).
- **Recommendation.** **shadcn/ui.** Standard answer for Tailwind + React in 2026. Owns the accessibility for us, doesn't lock us into a visual style, code lives in our repo so we can edit any component freely.
- **Why now (not later).** Once we pick this, B2's archetype mocks should be drawn assuming shadcn's primitive shapes. Picking later means redoing them.
- **What we produce.** A locked decision + a sub-list: which shadcn components we'll initially install, plus a short list of yaaos-specific composites we'll need to build on top (e.g. `ReviewCard`, `WorkflowTimeline`, `FindingRow`).
- **Term: composite component.** A higher-level React component built from primitives — like a `ReviewCard` made of `Card` + `Badge` + `Button`. Composites live in `domain/` or `shared/`; primitives live in `shared/components/ui/` (the shadcn convention).
- **Claude Design fit.** None for the decision itself. After locking, Claude Design's mocks should resemble shadcn's visual conventions (sober, clean, neutral) which works great with "modern, clean, crisp."

### D2. Design tokens `[ ]`

- **What it is.** Named values for color, type size, spacing, radius, motion, elevation. Used everywhere instead of raw values. Lock the tokens; every surface references them.
- **Terms.**
  - **Token** — a named CSS variable (`--color-text-primary`) or Tailwind class (`text-primary`) representing a design decision. Tokens are the "vocabulary" of the design system.
  - **Semantic token** — a token named by *role* (`--color-text-primary`), not by appearance (`--color-zinc-900`). When dark mode flips, the role stays, the value swaps. Better than appearance tokens.
- **Existing state.** `apps/web/src/styles.css` already has light/dark oklch color tokens. shadcn defaults use HSL CSS variables. We'll reconcile — likely keep oklch, map shadcn's variable names to ours.
- **What we produce.** A complete token set: color (semantic, ~20 roles), type scale (6–8 sizes), spacing scale (4px or 8px base, ~10 rungs), radius (3–5 rungs), motion (3 durations + 2 easings), elevation (3–4 shadows).
- **Claude Design fit.** Medium. Can sketch a palette and propose type pairings. Final values get tuned in code.

### D3. Iconography + voice `[ ]`

- **What it is.** Two small but pervasive choices.
  - **Iconography** — which icon set, what size scale, what stroke weight. Current SPA uses `lucide-react`. shadcn defaults to lucide too. Likely no change.
  - **Voice** — the writing style for microcopy: button labels, empty-state messages, error text, tooltips. "Delete review" vs "Remove this review" vs "Trash". Lock the tone — usually direct, plain, slightly warm — and write it down.
- **Why now.** Cheap to lock here, expensive to clean up later if every surface invented its own tone.
- **What we produce.** Icon set confirmed + size rungs (16/20/24). A short voice guide (5–10 rules + 3 paired examples of "yes / no" copy).
- **Claude Design fit.** None.

### D4. Accessibility baseline `[ ]`

- **What it is.** The floor of accessibility we hit on every surface. WCAG AA is the standard for SaaS products: contrast ratios, focus indicators, keyboard nav, screen-reader semantics.
- **Why now.** Cheap to bake in. Expensive to retrofit.
- **What we produce.** A short checklist applied to every primitive and every archetype: contrast ≥ 4.5:1 for body text, visible focus ring, tab order matches visual order, Escape closes overlays, Enter/Space activates buttons, all icons-only buttons carry `aria-label`. shadcn + Radix gives us 90% of this for free; we just need to not break it.
- **Claude Design fit.** None.

---

## Phase E — Surface design: apply to each

With everything above locked, design each real surface in the SPA. E2 splits into two passes — information design first (no visuals), then visual design (Claude Design mocks for anchors).

### E1. Priority order + scope per surface `[x]`

- **What it is.** Which surfaces are anchors (get Claude Design mocks) vs derived (follow archetypes + tokens).
- **Anchor set locked.** Dashboard, Ticket detail, Tickets list, Coding Agent detail.
- **What we produce.** Tiered ordering (Tier 0 Foundation → Tier 1 Anchors → Tier 2 high-touch derived → Tier 3 lower-traffic derived) + checkpoint between Tier 1 and Tier 2.
- **Claude Design fit.** None for E1; planning only.

### E2a. Per-surface information design `[ ]`

- **What it is.** For each surface, itemize the data displayed, hierarchy (primary / secondary / tertiary), available actions, supported states. Prose + tables. No visuals.
- **Why before Claude Design.** Visual mocks are worthless if we haven't decided what information they show. E2a grounds the mocks.
- **What we produce.** A spec block per surface: information inventory, hierarchy, actions, states, role-gated affordances.
- **Order.** Anchors first (Dashboard → Ticket detail → Tickets list → Coding Agent detail) — anchor info-design cascades to derived surfaces. Then Tier 2 derived. Then Tier 3 (brief).
- **Claude Design fit.** None — this is prose/tables.

### E2b. Per-surface visual design `[ ]`

- **What it is.** Claude Design mocks for the four Tier 1 anchors, based on E2a's locked information.
- **Prerequisites.** D1–D4 locked (tokens + primitives + voice + accessibility); E2a anchor info locked.
- **What we produce.** Mock per anchor (two mocks each for Dashboard + Ticket detail to cover state variants). Plus a short prose spec describing the mock's structure.
- **Claude Design fit.** **Strong** — this section is the primary use.
- **Checkpoint after.** Mandatory review of anchor mocks before deriving Tier 2 / Tier 3 visual specs.

---

## Phase F — Delivery: how it ships

### F1. Implementation slicing `[ ]`

- **What it is.** How the redesign lands in code without leaving the SPA half-old / half-new for weeks.
- **Options.**
  - **Big-bang branch** — one long-lived branch, full redesign, ship in one PR. Cleanest visual coherence; worst for review + rebase pain.
  - **Phased on main** — tokens + library first, then archetypes, then surface-by-surface. Each phase ships independently. SPA looks inconsistent mid-flight, but reviewable PRs.
  - **Parallel UI behind a flag** — new SPA at `/v2/*` while `/` stays old. Switch at the end. More plumbing, but no "broken in the middle."
- **What we produce.** A locked choice + a phase ledger that becomes `PHASES.md`.
- **Claude Design fit.** None.

### F2. Definition of done `[ ]`

- **What it is.** What "M06 complete" means concretely.
- **What we produce.** Checklist: every surface from A2 redesigned; every state pattern from C2 applied; AA accessibility verified on critical flows; docs updated (`apps/web/docs/`); `apps/web/bin/ci` + `apps/e2e/bin/ci` green.

---

## Working agreement

- **One section at a time.** We tackle them in order. Jumping ahead is allowed when a later decision is blocking, but the default is sequential.
- **Output of each section gets persisted** into `requirements.md` (or `architecture.md` if it's structural enough). The section's checkbox flips to `[x]` once persisted.
- **Decisions log.** Any decision where a clear alternative was rejected gets a one-line entry in `DECISIONS.md` with the "why." This is the milestone's audit trail, not a debate transcript.
- **Term sheet.** This file is the term sheet. If we use a design or React word that isn't defined here, we add it.
