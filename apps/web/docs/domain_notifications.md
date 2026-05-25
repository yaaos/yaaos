# domain/notifications

> Cross-org inbox page + the sidebar bell popover. Both surface the same backend module — `domain/notifications` in the backend.

## Purpose

The `/notifications` route + the sidebar `NotificationsBell` composite. The page renders the full chronological list with all/unread/read filter chips; the bell renders an unread-count badge + the latest 10 unread items in a popover.

## Public interface

- `NotificationsPage` — mounted by `core/routing` at `/notifications`. Single-component module.
- The sidebar bell + popover are the `NotificationsBell` chrome composite at `apps/web/src/shared/components/chrome/notifications-bell.tsx` (not under this domain folder because chrome is shared).

## Module architecture

### `/notifications` page

`apps/web/src/domain/notifications/index.tsx`:
- `PageHeader` with title + "Mark all read" action button.
- Filter chips (all / unread / read) — local state.
- List of `Row` items; each row click invokes `useMarkNotificationRead.mutate(id)`. Unread rows tint with `bg-accent/40` + bolded title.
- State patterns: `Skeleton` (5 placeholder rows) while loading; `EmptyState` (Bell icon) when filtered list is empty; no explicit error state — `useNotifications` retries on its own.

### `NotificationsBell` (chrome)

Lives at `apps/web/src/shared/components/chrome/notifications-bell.tsx`. Reads `useNotificationsPopover()` for `{items, unread_count}`. Badge shows the count (rendered as `99+` past 99). Popover lists up to 10 unread items + footer button for "Mark all read" (disabled when nothing's unread).

## Data owned

None. State lives in `core/api` query caches keyed `["notifications", read_state]` + `["notifications", "popover"]`.

## How it's tested

The chrome bell is exercised by the sidebar's existing test (which mocks `useNotificationsPopover` to return empty).
