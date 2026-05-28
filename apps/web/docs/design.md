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

Two-column split: fixed-width sidebar (220px pinned / 56px rail) + flex-grow `<main>`. Only `<main>` scrolls. Standalone routes (`/login`, `/user`, `/orgs`) render without the shell. React composition: [core_layout.md](core_layout.md).

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

Pages own their own header (`<h1>` or card header). The layout adds no top padding or chrome. [`PageHeader`](components.md) is opt-in. Long pages scroll inside `<main>`; everything outside stays anchored.

## Navigation model

- **One nav surface.** Sidebar only. Sub-page nav is in-content (tab list) or absent.
- **No breadcrumbs, no back-links.** Two clicks to anything; sidebar + browser back is enough. A "Back to X" link implies depth the IA doesn't have.
- **URL structure:** `/orgs/$slug/...` for org-scoped pages; `/`, `/login`, `/user/*`, `/orgs` are user-scoped or pre-auth.
- **Role gating:** items hidden when the user lacks the required role; groups disappear if no child survives. UI hint only — backend `require(action)` is the authority.

## State patterns

One shape per state — don't invent hybrids. See [components.md](components.md) for each primitive.

| State | Pattern |
|---|---|
| Empty | `EmptyState`: icon + headline ("No tickets yet") + optional CTA. Don't conflate with setup-required. |
| Loading | Skeleton for lists; inline spinner for mutations. No full-page spinners. |
| Error | `ErrorBanner`: in-page, optional Retry. Voice: system failed, not user. Validation errors (4xx field map) inline under the field. |
| Success | "Saving…" → "Saved." inline. Toasts only for cross-surface successes with no anchor control. |
| Live-stream | Activity pane (SSE) appends rows chronologically; new rows fade in 200ms. See [core_sse.md](core_sse.md). |
| Setup-required | `NotConfiguredBanner` at top of `<main>`. Dashboard replaces gated content with it; other pages show both. |

## Information density

Density is a feature. yaaos is a devtools product with many rows and many orgs in flight.

- Body root: 13px on `<html>`.
- Spacing: `p-2`/`p-3`/`p-4` inside cards + tables; `p-6` for top-of-page only.
- Tables over card grids for lists ≥ 5 items.
- One affordance per row — secondary actions go in the detail page or a row menu.
- Type scale: see [§ Type](#type). Avoid `text-2xl` unless it's a page-defining title.
- Surface discipline: lists = dense; detail + settings = comfortable; empty/error = airy.

## Voice

Direct, terse, engineering-honest. Operators, not consumers.

- Active over passive. System owns failure — never the user.
- Concrete over hedged: "Retry in 30s" not "Please try again later."
- No exclamation marks. No "Please." No emoji in product copy.
- Locked copy patterns:
  - Destructive confirm: "Delete \<thing>?" + "This can't be undone." + red `Delete`.
  - Cost confirm: "Run review?" + cost line + neutral `Run`.
  - Save success: "Saved." (period, no exclamation).
  - Empty list: "No \<things> yet." + optional CTA.
  - Error retry: "Couldn't load \<thing>. Retry?"

## Iconography

- Library: `lucide-react` only. Don't mix.
- Stroke: Lucide default (`stroke-width: 2`). No overrides.
- Sizes: `w-4 h-4` nav/rows · `w-5 h-5` buttons · `w-6 h-6` empty-state hero. No arbitrary values.
- Color: `currentColor` — no hardcoded fill/stroke.
- One icon per concept, used consistently.
- Decorative: `aria-hidden`. Meaningful: `aria-label` or `title`.

## Accessibility (a11y)

Target: WCAG 2.1 AA on critical flows.

- Radix/shadcn primitives cover focus management, ARIA roles, escape-to-close, focus-trap, keyboard nav — don't reimplement.
- Icon-only buttons get `aria-label` or `title`. Sidebar nav uses `title` for rail tooltips.
- Color is never the sole meaning carrier — pair with icon, label, or position.
- Focus ring: global `*:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px }`. Don't suppress.
- `prefers-reduced-motion` via Tailwind `motion-reduce:` variants.
- SSE-driven live regions use `aria-live="polite"`. Long lists use `<ul>/<li>` or `<table>`.
- `apps/e2e/` wires `axe-core` smoke checks.

## Design tokens

Semantic CSS variables defined in [`src/styles.css`](../src/styles.css), aliased onto Tailwind utilities in [`tailwind.config.ts`](../tailwind.config.ts). Theme swaps via `[data-theme="light"|"dark"]` on `<html>`; `:root` defaults to dark.

### Color — semantic roles

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

Sidebar-scoped tokens: `--sidebar-background`, `--sidebar-foreground`, `--sidebar-primary`, `--sidebar-primary-foreground`, `--sidebar-accent`, `--sidebar-accent-foreground`, `--sidebar-border`, `--sidebar-ring`.

Legacy tokens (`--bg`, `--surface`, `--text`, `--accent-2`, `--danger`, etc.) coexist for unmigrated surfaces — scheduled for removal; don't use in new code.

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

### Spacing + radius + motion

Values are in `tailwind.config.ts` and `src/styles.css`. No arbitrary values (`p-[7px]`) — add a rung or fix the inconsistency.

### Adding a token

Add to both `:root,[data-theme="dark"]` and `[data-theme="light"]` in `styles.css`, map in `tailwind.config.ts`, update the color table above.

## Related docs

- [components.md](components.md) — primitive + composite index. What's available; what each thing is for.
- [architecture.md](architecture.md) — layer model (core / domain / shared) and cross-cutting wiring.
- [patterns.md](patterns.md) — import rules, testid conventions, query keys, time helpers, SSE invalidation, dumb-frontend rules.
- [core_layout.md](core_layout.md) — React composition of the shell (`AppShell`, route outlet).
