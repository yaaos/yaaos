# domain/notifications

> Cross-org notification inbox and sidebar bell popover.

## Scope

- `/notifications` — `NotificationsPage`. Full list with all/unread/read filter chips; row click marks read.
- `NotificationsBell` — chrome composite at `apps/web/src/shared/components/chrome/notifications-bell.tsx`. Unread badge (99+ cap) + popover of up to 10 unread items + "Mark all read".

Consumes: `useNotifications`, `useNotificationsPopover`, `useMarkNotificationRead`. Query keys: `["notifications", read_state]`, `["notifications", "popover"]`. Owns no data.

## Tests

Bell covered by sidebar tests (mocks `useNotificationsPopover` to return empty). No dedicated page Vitest.
