# Design tokens

> Semantic CSS variables that every component reads. Two vocabularies share the same oklch values during M06: yaaos-named (legacy, scheduled for Phase 9 removal) and shadcn-named (canonical going forward).

Defined in [src/styles.css](../src/styles.css); aliased onto Tailwind utilities in [tailwind.config.ts](../tailwind.config.ts).

## Theme switching

Themes swap via `[data-theme="light"|"dark"]` on `<html>`. `:root` defaults to dark. Variable names stay; oklch values flip.

## Semantic colors (shadcn-named — canonical)

| Token | Purpose |
|---|---|
| `--background` / `--foreground` | Page background and default text. |
| `--card` / `--card-foreground` | Cards and elevated panels. |
| `--popover` / `--popover-foreground` | Popovers, dropdowns, tooltips. |
| `--primary` / `--primary-foreground` | Brand purple. Primary actions, focused states. |
| `--secondary` / `--secondary-foreground` | Subdued surfaces — toolbars, header strips. |
| `--muted` / `--muted-foreground` | De-emphasized text + surfaces (captions, helper text). |
| `--accent` / `--accent-foreground` | Hover/highlight surface inside menus, list rows, dropdowns. **Not** the brand color — that lives in `--primary`. |
| `--destructive` / `--destructive-foreground` | Destructive actions (Delete, Remove). |
| `--success` / `--success-foreground` | Positive state (badges, toasts). |
| `--warning` / `--warning-foreground` | Cautionary state. |
| `--info` / `--info-foreground` | Informational state. |
| `--border` | Default 1px border color. |
| `--input` | Form-control border. |
| `--ring` | Focus-ring color — applied at the global `*:focus-visible` rule plus shadcn primitives. |
| `--radius` | Component corner radius (6px). Tailwind `rounded` resolves to this. |

### Sidebar-scoped (consumed by shadcn `sidebar` primitive)

| Token | Notes |
|---|---|
| `--sidebar-background` / `--sidebar-foreground` | Sidebar surface + default text. |
| `--sidebar-primary` / `--sidebar-primary-foreground` | Active item / brand affordance inside the sidebar. |
| `--sidebar-accent` / `--sidebar-accent-foreground` | Hovered sidebar items. |
| `--sidebar-border` | Section dividers inside the sidebar. |
| `--sidebar-ring` | Sidebar focus-ring color. |

## Legacy yaaos-named (transitional)

Kept side-by-side with shadcn-named through M06 so unmigrated surfaces keep rendering. Phase 9 deletes these and rewrites every reference.

`--bg`, `--bg-2`, `--surface`, `--surface-2`, `--surface-3`, `--hover`, `--border-soft`, `--border-hard`, `--text`, `--text-2`, `--text-3`, `--text-4`, `--accent-2`, `--accent-dim`, `--accent-bg`, `--accent-bg-2`, `--accent-border`, `--danger`.

## Type scale

Body root is 13px (`html { font-size: 13px }`) — compact density per requirements.md C3.

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

## Spacing scale

Tailwind defaults; common rungs: `1` (4px), `2` (8px), `3` (12px), `4` (16px), `6` (24px), `8` (32px), `12` (48px), `16` (64px). No arbitrary values (`p-[7px]`) — add a rung or fix the inconsistency.

## Radius

| Class | Value |
|---|---|
| `rounded-sm` | 4px |
| `rounded` | 6px (`var(--radius)`) |
| `rounded-md` | 8px |
| `rounded-lg` | 10px |
| `rounded-card` | 10px (legacy alias) |
| `rounded-pill` | 9999px |

## Motion

| Class | Value | Use |
|---|---|---|
| `duration-100` | 100ms | Hover, focus. |
| `duration-200` | 200ms | Open/close. |
| `duration-400` | 400ms | Rare; expanding panels. |

`prefers-reduced-motion` honored via Tailwind's `motion-reduce:` variants on animated primitives.

## Focus ring

Global rule (`src/styles.css`):

```
*:focus-visible {
  outline: 2px solid var(--ring);
  outline-offset: 2px;
}
```

Primitives keep their internal `focus-visible:ring-2 focus-visible:ring-ring` for explicit treatment. Both compose without double-drawing.

## Adding a new token

1. Add the variable to both `:root,[data-theme="dark"]` and `[data-theme="light"]` blocks in `styles.css`.
2. Map it in `tailwind.config.ts` so a utility class exists.
3. Update this doc.
