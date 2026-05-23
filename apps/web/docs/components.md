# Components

> Index of the React primitives + composites available in the SPA. Domain-specific composites live in their feature module and aren't listed here.

## Layers

```
src/shared/components/
├── ui/        shadcn primitives (one file per primitive; copied in via the shadcn CLI)
├── chrome/    yaaos chrome composites — sidebar, org switcher, user popover, notifications (Phase 2+)
└── layout/    yaaos layout composites — page header, empty state, error banner (Phase 2+)
```

Primitives are thin wrappers over Radix UI (focus management, ARIA correctness) and Tailwind (visual style via [design tokens](design-tokens.md)). They live in our repo — modify freely.

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
| `dialog.tsx` | Modal dialog. Composed by ConfirmModal / PickerModal in Phase 2. |
| `popover.tsx` | Anchored floating panel. Used by Org switcher, Notifications. |
| `dropdown-menu.tsx` | Menu list anchored to a trigger. |
| `tooltip.tsx` | Hover/focus tooltip. |
| `sheet.tsx` | Side-anchored drawer. Required transitively by the shadcn `sidebar` primitive's mobile collapse — yaaos's M06 navigation doesn't expose a drawer pattern. |

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
| `sidebar.tsx` | shadcn sidebar primitive — handles collapse, sub-items, mobile-sheet fallback. yaaos composes a `Sidebar` on top in Phase 2. |
| `collapsible.tsx` | Inline expand/collapse panel. |
| `scroll-area.tsx` | Custom-scrollbar viewport. |

### Toast

| File | Purpose |
|---|---|
| `sonner.tsx` | Wraps `sonner` for theme-aware toasts. Rendered once in `main.tsx`. |

## Hooks (`src/shared/hooks/`)

| File | Purpose |
|---|---|
| `use-mobile.tsx` | Returns `true` when the viewport is below the mobile breakpoint. Used by shadcn `sidebar`. |

## Legacy primitives (`src/shared/components/*.tsx`)

`button.tsx`, `badge.tsx`, `card.tsx`, `dialog.tsx`, `placeholder-page.tsx` are the M01–M05 hand-rolled primitives. They keep working through Phases 1–8 so unmigrated surfaces still render; Phase 9 deletes them once every caller is on the new primitives.

## Adding a primitive

1. `pnpm dlx shadcn@latest add <name> --yes` (writes to `src/shared/components/ui/<name>.tsx` and installs any Radix dep).
2. If shadcn's CLI rewrites `tailwind.config.ts` or `src/styles.css`, reconcile so both yaaos-named and shadcn-named token layers stay intact.
3. Update this doc with a one-liner.
