# Components

> Index of the React primitives + composites available in the SPA. Domain-specific composites live in their feature module and aren't listed here.

## Three-layer model

| Layer | Location | What lives here |
|---|---|---|
| **Vendor / primitive** | `src/shared/components/ui/` | Vendored shadcn/Radix primitives. No domain logic, no restyling inside a primitive — wrap in a composite instead. |
| **Composite** | `src/shared/components/public/{layout,chrome}/` + `public/` root | Presentational, cross-feature composites (`PageHeader`, `EmptyState`, `ErrorBanner`, `OrgSwitcher`, `Markdown`, …). No feature-specific data fetching. |
| **Feature** | `src/domain/<module>/` | Domain-specific components that colocate with their module. Graduate to composite on the 2nd/3rd consumer (rule-of-three). |

**Rule-of-three graduation:** a feature component moves to `shared/components/` once it has real consumers in two or more unrelated domain modules. Don't pre-graduate — leave it in `domain/<m>/` until it earns its place.

**Vendor-layer carve-out:** shadcn/Radix primitives in `ui/` may hand-roll ARIA patterns and focus management internally — that's the vendor's job, not ours. Don't add domain logic or hardcoded copy inside those files.

**`tw-animate-css`** (`src/styles.css`, imported right after `@import "tailwindcss"`) supplies the `animate-in`/`animate-out`/`fade-*`/`zoom-*`/`slide-*` utilities every overlay primitive (`dialog.tsx`, `alert-dialog.tsx`, `sheet.tsx`, `select.tsx`, `dropdown-menu.tsx`, `popover.tsx`) references for its open/close transition. It's the Tailwind v4-native port of the (Tailwind v3-only) `tailwindcss-animate` plugin these shadcn components assume. Without it those utility classes resolve to no CSS at all — Radix's `Presence` (which every overlay uses to defer unmounting until its closing CSS animation finishes) then waits forever for an animation that never started, so the overlay's backdrop stays mounted and clickable indefinitely after "closing," silently blocking every later click on the page. Required whenever adding a new overlay-family primitive.

`src/shared/components/`: `ui/` (shadcn/Radix primitives), `public/layout/` (page header, empty state, error banner), `public/` root (content composites — `markdown.tsx`). All live in-repo — modify freely. The chrome components (`OrgSwitcher`, `NotificationsBell`) and the org-gate banner (`NotConfiguredBanner`) moved to `core/sidebar/` and `core/layout/public/` respectively — they use `@core/api` hooks and so cannot live in `shared/`.

## Primitives (`src/shared/components/ui/`)

### Form

| File | Purpose |
|---|---|
| `button.tsx` | All clickable affordances. Variants: `default`, `destructive`, `outline`, `secondary`, `ghost`, `link`. |
| `input.tsx` | Single-line text inputs. |
| `textarea.tsx` | Multi-line text inputs. |
| `select.tsx` | Native-feel dropdown select, Radix-driven. |
| `checkbox.tsx` | Boolean field. |
| `radio-group.tsx` | Single-choice field among a small fixed set (e.g. pipeline stage boundary mode). |
| `switch.tsx` | Boolean toggle (visually distinct from `checkbox.tsx` — used for on/off settings like a stage's review loop). |
| `label.tsx` | Form labels — associates via `htmlFor`. |
| `form.tsx` | `react-hook-form` integration (FormField, FormItem, FormControl, FormMessage). |

### Overlays

| File | Purpose |
|---|---|
| `alert-dialog.tsx` | Destructive or high-stakes confirmation modal (Radix `AlertDialog`). No close X — use `AlertDialogCancel` / `AlertDialogAction` buttons. Used by `ShutdownDialog`, `CancelShutdownDialog`, and the Repos settings page's protected-code mode-switch confirm. |
| `dialog.tsx` | General-purpose modal dialog. Composed by ConfirmModal. |
| `sheet.tsx` | Right-anchored slide-in panel (built on `@radix-ui/react-dialog`, no separate package). Used by the ticket Runs tab's per-stage artifact viewer and the Pipelines settings page's per-stage editor. |
| `popover.tsx` | Anchored floating panel. Used by Org switcher, Notifications. |
| `dropdown-menu.tsx` | Anchored action menu (Radix `DropdownMenu`). Used by the Pipelines settings page's per-stage row actions and "Add stage" picker. |
| `command.tsx` | Filterable list (`cmdk`). Paired with `popover.tsx` for multi-select pickers — the Repos settings page's notify/owner user pickers. |

### Display

| File | Purpose |
|---|---|
| `table.tsx` | Semantic table primitives (`Table`, `TableHeader`, `TableRow`, `TableCell`, …). |
| `badge.tsx` | Status pills. Variants: `default`, `secondary`, `destructive`, `outline`. |
| `skeleton.tsx` | Loading placeholder. |
| `accordion.tsx` | Multi-item expand/collapse list (Radix `Accordion`). Used by the Pipelines settings page's pipeline list. Prefer native `<details>` (see `runs.tsx`'s `RunCard`) for a single ad-hoc disclosure — reach for `Accordion` only when the list needs single-open-at-a-time semantics. |
| `collapsible.tsx` | Single expand/collapse disclosure (Radix `Collapsible`). Used by the Pipelines settings page's per-stage "Advanced settings" section. |

### Toast

| File | Purpose |
|---|---|
| `sonner.tsx` | Wraps `sonner` for theme-aware toasts. Rendered once in `main.tsx`. |

## Layout composites (`src/shared/components/public/layout/`)

Public surface of the `shared/components` module. Import directly via `@shared/components/public/layout/<file>`.

| File | Export | Purpose |
|---|---|---|
| `page-header.tsx` | `PageHeader` | Title + optional subtitle + right-aligned actions slot. The first composite on every surface. |
| `empty-state.tsx` | `EmptyState` | Icon + headline + body + optional action; the C2 empty-list pattern. |
| `error-banner.tsx` | `ErrorBanner` | In-page error with optional Retry. Voice rule (D3): blames the system, not the user. |
| `confirm-modal.tsx` | `ConfirmModal`, `ConfirmTone` | Destructive + cost-protective variants share the shell; copy differs (D3). |
| `org-settings-layout.tsx` | `OrgSettingsLayout` | Passthrough shell for Org Settings sub-pages (no top chrome). Graduated from `domain/org_settings` once `domain/pipeline_settings` needed the same shell (rule-of-three). |

## Content composites (`src/shared/components/public/`)

| File | Export | Purpose |
|---|---|---|
| `markdown.tsx` | `Markdown` | Renders a model-generated body (pipeline artifacts, run failure text) via `react-markdown` + `rehype-sanitize` — sanitization always runs, no opt-out. No typography plugin installed; block-element styling is hand-rolled via Tailwind arbitrary-child selectors. |

## Adding a primitive

`pnpm dlx shadcn@latest add <name> --yes`. If the CLI rewrites `src/styles.css` or attempts to add a `tailwind.config.ts`, discard the config file (Tailwind v4 is CSS-first; no JS config) and reconcile any token additions against the `@theme` + `@layer base` structure in `src/styles.css`. Add a one-liner to this doc.
