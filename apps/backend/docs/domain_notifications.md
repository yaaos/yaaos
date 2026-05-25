# domain/notifications

> Cross-org user inbox. One row per user-targeted event; the SPA renders unread items in the sidebar bell + the `/notifications` page (E2a.6 / E2a.7).

## Purpose

Owns the `notifications` table + read/write API for the SPA's bell + inbox. Idempotent writes deduplicate re-emitted workflow transitions so the user inbox never doubles up.

## Public interface

Exported from `app/domain/notifications/__init__.py`:

- Types — `Notification`, `NotificationRow`.
- Service — `record(...)`, `list_for_user(...)`, `popover_for_user(...)`, `mark_read(...)`, `mark_all_read(...)`.

HTTP routes (`/api/notifications`):

| Method | Path                                   | Auth                  |
|--------|----------------------------------------|-----------------------|
| GET    | `/api/notifications`                   | session cookie only   |
| POST   | `/api/notifications/{id}/read`         | session cookie only   |
| POST   | `/api/notifications/mark-read`         | session cookie only   |
| GET    | `/api/notifications/popover`           | session cookie only   |

All four endpoints classify as `RouteSecurity.USER_SCOPED` (cross-org). The prefix lives in `USER_SCOPED_PREFIXES` in `core/auth/types.py`; the middleware does not demand `X-Org-Slug` and handlers resolve the session cookie themselves (same pattern as `/api/orgs/mine`).

## Module architecture

### Entities

- `NotificationRow` (`notifications` table) — `(user_id, org_id, type, ticket_id?, title, body, read_at?, created_at)`.
- The `type` column is freeform text — today the writers emit `hitl_waiting`, `ticket_completed`, `ticket_failed`; future workflow transitions slot in without a migration.

### Key indexes

- `(user_id, read_at, created_at desc)` — the primary read path ("unread + recent for me").
- `(user_id, org_id, type, created_at desc)` — per-org / per-type filter combinations.

### Core flows

- **Record.** `record(user_id, org_id, type, title, body, ticket_id?, session)` — idempotent by `(user_id, type, ticket_id)`. Re-emitting the same workflow transition is a no-op (returns `None`).
- **Read.** `list_for_user(...)` filters by `read_state` (`all` / `unread` / `read`), optional `org_id`, optional `types`. `popover_for_user(...)` returns the latest N unread items + the unread count for the sidebar bell.
- **Mark read.** `mark_read(notification_id)` flips `read_at` to now if null (idempotent). `mark_all_read(org_id?, types?)` does a single bulk UPDATE.

### No workflow subscribers wired today

`core/events` subscriptions that would turn workflow transitions into notification rows aren't wired. The schema, endpoints, and SPA wiring exist; no producer emits rows in normal operation.

## Data owned

- `notifications` table (created by migration `021_create_notifications`).

## How it's tested

`apps/backend/app/domain/notifications/test/test_endpoints.py` — service tests against a real DB session covering unauth 401, per-user scoping (Alice's list doesn't surface Bob's rows and vice versa), popover unread_count, `mark_read` idempotency, `mark_all_read` scoping, and `service.record()` idempotency by `(user_id, type, ticket_id)`.
