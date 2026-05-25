# Design

> The yaaos SPA's design system in one doc — principles, layout, navigation, state patterns, density, voice, icons, accessibility, and the design-token vocabulary. Read this before adding a surface, a chrome element, or a new pattern. Cross-links into [patterns.md](patterns.md) (frontend code patterns) and [components.md](components.md) (primitive index).

## Principles

These are the rules the SPA is built on. They are absolute — if a design or change conflicts with one, fix the design, not the rule.

- **No top bar. Ever.** All chrome lives in the sidebar (logo, org switcher, primary nav, notifications, user card, theme/pin toggle). The main content area's topmost element is the page itself. No horizontal strips above pages — no breadcrumbs strip, no tab bar, no toolbar. *Why:* yaaos is a devops tool with high information density; vertical real estate is precious. Mirrored chrome (sidebar + topbar) wastes pixels and dilutes hierarchy.
- **Sidebar is the only persistent nav.** Active section is communicated by the sidebar's highlight and (for groups) its expanded state. Sub-pages do not get tab strips or secondary navs; they own the full content area.
- **IA stays shallow.** Two clicks to anything. No breadcrumbs (would imply depth we don't have). No back-links — sidebar + browser back is sufficient.
- **Page content is the topmost element.** Every page's first paint is its own header — usually an `h1` or section card. No global page-title strip provided by a layout.
- **Dumb frontend.** The SPA renders data and dispatches actions. Verdicts, counts, statuses, and permissions come from the server. See [patterns.md § Dumb frontend](patterns.md#dumb-frontend).
- **Density first.** Compact spacing and 13px body text are defaults, not exceptions. See [§ Information density](#information-density).
- **One pattern per state.** Empty / loading / error / success follow the locked shapes in [§ State patterns](#state-patterns). Don't invent a new spinner or a custom empty illustration mid-feature.
- **Consistency over novelty.** A change that looks better in isolation but doesn't fit existing patterns goes back to the design layer — update the pattern repo-wide or don't ship the change.

## Layout

### Shell

The signed-in app is a single horizontal split:

```
┌───────────┬─────────────────────────────┐
│ Sidebar   │ Main content                │
│ (220px /  │ (flex-1, only this scrolls) │
│  56px     │                             │
│  in rail) │                             │
└───────────┴─────────────────────────────┘
```

- The sidebar is fixed-width and never scrolls horizontally. Pinned width: 220px. Unpinned (rail) width: 56px — icons only.
- Only `<main>` scrolls. The sidebar and any in-page banners stay put.
- Standalone routes (`/login`, `/user`, `/orgs`) render outside the shell — no sidebar — because they are user-scoped or pre-auth.
- See [core_layout.md](core_layout.md) for the React composition (`AppShell`, route outlet, broken-integrations banner).

### Sidebar

Top-to-bottom anatomy:

1. **Brand lockup** — full `yaaos` lockup when pinned; mark only when in rail.
2. **Org switcher chip** — defines the current org context. Click for a popover of other orgs + a "View all orgs" link. Divider beneath separates it from nav.
3. **Org-scoped nav** — Dashboard, Tickets, Lessons, Org Settings (group).
   - Top-level items are links. Groups expand inline to show sub-items.
   - **Auto-collapse rule:** a group is expanded only while one of its children is the active route. Navigating away closes it. The user can still manually toggle from anywhere; the next route change re-applies the rule.
   - **Rail-mode flyout:** in unpinned mode, the group icon opens a right-anchored popover with the sub-items — sub-items remain reachable without re-pinning.
   - **Centered icons in rail:** every nav button (and the org switcher) centers its icon when the sidebar is unpinned.
   - **No layout shift on select:** the active item is indicated by background only — no border, no margin/padding change.
4. **Notifications bell** — user-scoped (cross-org). Popover with unread items + Mark-all-read.
5. **User card** — avatar + name + `@handle`. Opens a popover with Details, Security, theme toggle, Log off.
6. **Footer rail** — version pill + pin/unpin toggle.

### Page region

Inside `<main>`:

- Pages own their own header (usually `<h1>` or a card header). There is no shared "PageHeader" strip mandated by the layout — the [`PageHeader`](components.md) composite is opt-in for surfaces that benefit from a title + actions row.
- Vertical rhythm: pages set their own top padding (`p-6` is common for max-width content columns). The layout adds none of its own.
- Long pages scroll inside `<main>`; everything outside `<main>` stays anchored.

## Navigation model

- **One nav surface.** The sidebar is the only navigation chrome. Sub-page nav (settings sub-pages, ticket-detail tabs, etc.) is either part of the page content (an in-content tab list) or absent (linked from the sidebar group).
- **No breadcrumbs.** IA depth never exceeds two levels of org-scoped + the page itself. Breadcrumbs would imply depth we don't have.
- **No back-links.** Sidebar nav + browser back is enough. A "Back to X" link is a code smell suggesting the page belongs deeper than IA allows.
- **URL structure:** `/orgs/$slug/...` for every org-scoped page. `/`, `/login`, `/user/*`, `/orgs` are user-scoped or pre-auth.
- **Role gating in nav:** items are hidden when the user lacks the role required for the destination. Groups disappear entirely if no child survives the filter. Mirrors backend `require(action)` — the sidebar is a UI hint, not the authority.

## State patterns

Every surface that fetches or mutates data follows one shape per state. Pick the right one; do not invent a hybrid.

### Empty (no data, no error)

- Use `EmptyState` ([components.md](components.md)): icon, headline, optional body, optional primary CTA.
- Headline says what's missing in user terms ("No tickets yet"). Body is one line of context.
- Don't conflate "empty" with "not configured" — see [§ Setup-required gate](#setup-required-gate).

### Loading

- Skeleton placeholders for list shapes; a small inline spinner for in-flight mutations.
- No full-page spinners. No skeleton "shimmer" that lasts longer than 200ms before content appears.

### Error

- Use `ErrorBanner` ([components.md](components.md)): in-page banner, body text, optional Retry.
- Voice rule: the system failed, not the user. Don't say "You did X wrong"; say "Couldn't load X. Retry?". See [§ Voice](#voice).
- Validation errors (4xx with a field-keyed map) surface inline under the relevant input — they're not error banners.

### Success

- Mutations show "Saving…" → "Saved." inline near the action, not as a toast.
- Toasts are reserved for cross-surface successes that aren't anchored to a control (rare).

### Live-stream

- Ticket detail's Activity pane (SSE) streams new rows in chronologically. New rows fade in for 200ms then settle — no popping. See [core_sse.md](core_sse.md) for the subscription model.

### Setup-required gate

- An org that isn't fully configured (missing VCS, etc.) shows `NotConfiguredBanner` at the top of `<main>`. Admins see a missing-piece list; non-admins see "ask your admin."
- The Dashboard treats setup-required as its primary state — gated content is replaced by the banner. Other pages render their normal empty state alongside the banner.

## Information density

yaaos is a tool for engineers running many tickets and many orgs. Density is a feature, not a tradeoff.

- **Body root: 13px.** Set on `<html>`; everything else cascades.
- **Compact spacing rungs.** Use `p-2` (8px) / `p-3` (12px) / `p-4` (16px) inside cards and tables. `p-6` (24px) is for top-of-page padding only.
- **Tables over cards for lists ≥ 5 items.** A 20-row card grid wastes pixels.
- **One affordance per row.** A list row links to the detail page; secondary actions live in the detail page or a row menu, not a button cluster on the row.
- **Tight type scale** — see [§ Type](#type) below. Don't reach for `text-2xl` unless it's a page-defining title.
- **Discipline by surface type:**
  - List pages: dense; many rows visible without scroll.
  - Detail pages: comfortable; one focused decision at a time.
  - Settings forms: comfortable; clear field grouping, generous label spacing.
  - Empty/error states: airy; let the message breathe.

## Voice

The SPA's voice is direct, terse, and engineering-honest. We're talking to operators, not consumers.

- **Active over passive.** "Couldn't reach GitHub" beats "GitHub could not be reached."
- **System owns failure.** Errors blame the system or the network; never the user.
- **Concrete over hedged.** "Retry in 30s" beats "Please try again later."
- **No exclamation marks.** Ever.
- **No emoji in product copy.** Reserved for things like coding-agent identity (Claude's robot, etc.) where the icon is the brand.
- **No "Please."** It's noise — "Save changes" not "Please save your changes."
- **Locked patterns:**
  - Destructive confirm: "Delete <thing>?" + "This can't be undone." + red `Delete` button.
  - Cost-protective confirm: "Run review?" + cost line ("Uses ~$0.12 of your BYOK budget.") + neutral `Run` button.
  - Save success: "Saved." (period, no exclamation).
  - Empty list: "No <things> yet." + optional next-step CTA.
  - Error retry: "Couldn't load <thing>. Retry?" with a Retry button.

## Iconography

- **Library:** [`lucide-react`](https://lucide.dev). Don't mix icon libraries.
- **Stroke weight:** Lucide default (`stroke-width: 2`). No per-icon overrides.
- **Sizes:** `w-4 h-4` (16px) is the default in dense nav/rows; `w-5 h-5` for buttons; `w-6 h-6` for empty-state hero icons. Avoid arbitrary sizes.
- **Color:** icons inherit `currentColor`. Don't hardcode fill or stroke colors — they should always pick up the parent's text color so theme switches work.
- **One icon per concept.** If "Tickets" is `Ticket`, it's always `Ticket` — don't substitute `ClipboardList` elsewhere.
- **Decorative icons get `aria-hidden`; meaningful ones get a `title` or sibling label.** See [§ Accessibility](#accessibility).

## Accessibility (a11y)

We target WCAG 2.1 AA on critical flows. The discipline here keeps us there.

- **shadcn/Radix primitives handle most of it.** Focus management, ARIA roles, escape-to-close, focus-trap inside dialogs, keyboard nav for menus — all built-in. Don't reimplement these.
- **What we add:**
  - Every interactive element is keyboard-reachable in a sensible order. Tab visits the sidebar then the main content; arrow keys navigate within menus/listboxes.
  - Buttons that have only an icon get an `aria-label` or a `title`. The sidebar nav uses `title` for tooltips when collapsed.
  - Color is not the sole carrier of meaning. Pair color with icon, label, or position (e.g., a red badge also says "Failed").
  - Focus ring is global: `*:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px }`. Don't suppress it.
  - `prefers-reduced-motion` honored on animated primitives (Tailwind `motion-reduce:` variants).
- **Per-surface gotchas:**
  - Live regions for SSE-driven updates (notifications, activity) use `aria-live="polite"` so screen readers don't get spammed.
  - Long lists use semantic `<ul>/<li>` or `<table>` — not `<div>` soup.
- **Verification:** `apps/e2e/` wires `axe-core` smoke checks.

## Design tokens

Semantic CSS variables that every component reads. Defined in [`src/styles.css`](../src/styles.css); aliased onto Tailwind utilities in [`tailwind.config.ts`](../tailwind.config.ts).

### Theme switching

Themes swap via `[data-theme="light"|"dark"]` on `<html>`. `:root` defaults to dark. Variable names stay constant; oklch values flip.

### Color — semantic roles (shadcn-named, canonical)

| Token | Purpose |
|---|---|
| `--background` / `--foreground` | Page background and default text. |
| `--card` / `--card-foreground` | Cards and elevated panels. |
| `--popover` / `--popover-foreground` | Popovers, dropdowns, tooltips. |
| `--primary` / `--primary-foreground` | Brand purple. Primary actions, focused states. |
| `--secondary` / `--secondary-foreground` | Subdued surfaces — toolbars, header strips. |
| `--muted` / `--muted-foreground` | De-emphasized text + surfaces (captions, helper text). |
| `--accent` / `--accent-foreground` | Hover/highlight surface inside menus, list rows, dropdowns. **Not** the brand color — that's `--primary`. |
| `--destructive` / `--destructive-foreground` | Destructive actions (Delete, Remove). |
| `--success` / `--success-foreground` | Positive state (badges, toasts). |
| `--warning` / `--warning-foreground` | Cautionary state. |
| `--info` / `--info-foreground` | Informational state. |
| `--border` | Default 1px border color. |
| `--input` | Form-control border. |
| `--ring` | Focus-ring color — applied at the global `*:focus-visible` rule plus shadcn primitives. |
| `--radius` | Component corner radius (6px). Tailwind `rounded` resolves to this. |

Sidebar-scoped tokens (consumed by the shadcn `sidebar` primitive): `--sidebar-background`, `--sidebar-foreground`, `--sidebar-primary`, `--sidebar-primary-foreground`, `--sidebar-accent`, `--sidebar-accent-foreground`, `--sidebar-border`, `--sidebar-ring`.

Legacy yaaos-named tokens (`--bg`, `--surface`, `--text`, `--accent-2`, `--danger`, etc.) coexist for unmigrated surfaces. They're scheduled for removal — don't reach for them in new code.

### Type

Body root is 13px (`html { font-size: 13px }`).

| Tailwind class | Size | Use |
|---|---|---|
| `text-xs` | 11px | Captions, badge text. |
| `text-sm` | 12px | Helper text, secondary labels. |
| `text-base` | 13px | Body default. |
| `text-lg` | 14px | Emphasized body, h4. |
| `text-xl` | 16px | h3. |
| `text-2xl` | 20px | h2. |
| `text-3xl` | 26px | h1. |

Font family: Geist (sans + mono). Mono uses tabular-nums.

### Spacing

Tailwind defaults; common rungs: `1` (4px), `2` (8px), `3` (12px), `4` (16px), `6` (24px), `8` (32px), `12` (48px), `16` (64px). No arbitrary values (`p-[7px]`) — add a rung or fix the inconsistency.

### Radius

| Class | Value |
|---|---|
| `rounded-sm` | 4px |
| `rounded` | 6px (`var(--radius)`) |
| `rounded-md` | 8px |
| `rounded-lg` | 10px |
| `rounded-pill` | 9999px |

### Motion

| Class | Value | Use |
|---|---|---|
| `duration-100` | 100ms | Hover, focus. |
| `duration-200` | 200ms | Open/close. |
| `duration-400` | 400ms | Rare; expanding panels. |

`prefers-reduced-motion` honored via Tailwind's `motion-reduce:` variants.

### Focus ring

```
*:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
```

Primitives keep their internal `focus-visible:ring-2 focus-visible:ring-ring` for explicit treatment. Both compose without double-drawing.

### Adding a new token

1. Add the variable to both `:root,[data-theme="dark"]` and `[data-theme="light"]` blocks in `styles.css`.
2. Map it in `tailwind.config.ts` so a utility class exists.
3. Update the table above.

## Related docs

- [components.md](components.md) — primitive + composite index. What's available; what each thing is for.
- [patterns.md](patterns.md) — frontend code patterns: module docs template, query keys, time helpers, SSE invalidation, dumb-frontend rules.
- [core_layout.md](core_layout.md) — React composition of the shell (`AppShell`, route outlet).
- [modularity.md](modularity.md) — layer shape and import rules.
