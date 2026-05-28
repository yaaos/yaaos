# Components

> Index of the React primitives + composites available in the SPA. Domain-specific composites live in their feature module and aren't listed here.

## Layers

`src/shared/components/`: `ui/` (shadcn/Radix primitives), `chrome/` (sidebar, org switcher, notifications), `layout/` (page header, empty state, error banner). All live in-repo — modify freely.

## Primitives (`src/shared/components/ui/`)

### Form

| File | Purpose |
|---|---|
| `button.tsx` | All clickable affordances. Variants: `default`, `destructive`, `outline`, `secondary`, `ghost`, `link`. |
| `input.tsx` | Single-line text inputs. |
| `textarea.tsx` | Multi-line text inputs. |
| `select.tsx` | Native-feel dropdown select, Radix-driven. |
| `checkbox.tsx` | Boolean field. |
| `switch.tsx` | Boolean field — preferred over Checkbox for instant-apply settings. |
| `label.tsx` | Form labels — associates via `htmlFor`. |
| `form.tsx` | `react-hook-form` integration (FormField, FormItem, FormControl, FormMessage). |

### Overlays

| File | Purpose |
|---|---|
| `dialog.tsx` | Modal dialog. Composed by ConfirmModal / PickerModal. |
| `popover.tsx` | Anchored floating panel. Used by Org switcher, Notifications. |
| `dropdown-menu.tsx` | Menu list anchored to a trigger. |
| `tooltip.tsx` | Hover/focus tooltip. |
| `sheet.tsx` | Side-anchored drawer. Required transitively by the shadcn `sidebar` primitive's mobile collapse — yaaos's navigation doesn't expose a drawer pattern. |

### Display

| File | Purpose |
|---|---|
| `table.tsx` | Semantic table primitives (`Table`, `TableHeader`, `TableRow`, `TableCell`, …). |
| `badge.tsx` | Status pills. Variants: `default`, `secondary`, `destructive`, `outline`. |
| `avatar.tsx` | Initials avatar with optional image fallback. |
| `separator.tsx` | Horizontal/vertical divider. |
| `skeleton.tsx` | Loading placeholder. |
| `tabs.tsx` | In-page tab navigation. |

### Layout

| File | Purpose |
|---|---|
| `sidebar.tsx` | shadcn sidebar primitive — handles collapse, sub-items, mobile-sheet fallback. yaaos composes a `Sidebar` on top. |
| `collapsible.tsx` | Inline expand/collapse panel. |
| `scroll-area.tsx` | Custom-scrollbar viewport. |

### Toast

| File | Purpose |
|---|---|
| `sonner.tsx` | Wraps `sonner` for theme-aware toasts. Rendered once in `main.tsx`. |

## Chrome composites (`src/shared/components/chrome/`)

| File | Purpose |
|---|---|
| `org-switcher.tsx` | `OrgSwitcher` — sidebar chip showing the current org with a dropdown of the user's other orgs + a "View all organizations" link to `/orgs`. Data via `useMyOrgs()`. |
| `notifications-bell.tsx` | `NotificationsBell` — Bell icon row with unread-count badge + popover. Renders a placeholder empty-state today; no live data wired. |

## Layout composites (`src/shared/components/layout/`)

| File | Purpose |
|---|---|
| `page-header.tsx` | `PageHeader` — title + optional subtitle + right-aligned actions slot. The first composite on every surface. |
| `empty-state.tsx` | `EmptyState` — icon + headline + body + optional action; the C2 empty-list pattern. |
| `error-banner.tsx` | `ErrorBanner` — in-page error with optional Retry. Voice rule (D3): blames the system, not the user. |
| `confirm-modal.tsx` | `ConfirmModal` — destructive + cost-protective variants share the shell; copy differs (D3). |
| `picker-modal.tsx` | `PickerModal` — "Add X" flows (plugin type, integration provider). Lists `PickerOption[]`; caller wires the post-pick route push. |
| `not-configured-banner.tsx` | `NotConfiguredBanner` — non-intrusive setup-required banner. Reads `useConfigStatus()`; shows the missing-piece list to Admins and "ask your admin" to Builders. Auto-hides when `configured: true`. |

## Hooks (`src/shared/hooks/`)

| File | Purpose |
|---|---|
| `use-mobile.tsx` | Returns `true` when the viewport is below the mobile breakpoint. Used by shadcn `sidebar`. |

## Adding a primitive

`pnpm dlx shadcn@latest add <name> --yes`. If the CLI rewrites `tailwind.config.ts` or `src/styles.css`, reconcile against the existing token layer. Add a one-liner to this doc.
