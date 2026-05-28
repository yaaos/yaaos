# domain/notifications

> Cross-org user inbox. One row per user-targeted event; the SPA renders unread items in the sidebar bell + the `/notifications` page (E2a.6 / E2a.7).

## Purpose

Owns the `notifications` table + read/write API for the SPA's bell + inbox. Idempotent writes deduplicate re-emitted workflow transitions so the user inbox never doubles up.

## Public interface

Exported from `app/domain/notifications/__init__.py`:

- Types — `Notification`.
- Service — `record(...)`, `list_for_user(...)`, `popover_for_user(...)`, `mark_read(...)`, `mark_all_read(...)`.
- Task ref — `handle_ticket_status_change` (`TaskRef`). Producers `enqueue` it; it writes one notification row per supplied `member_user_ids` entry.

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

- `notifications` table — `(user_id, org_id, type, ticket_id?, title, body, read_at?, created_at)`. Public value object: `Notification`.
- The `type` column is freeform text — today the writers emit `hitl_waiting`, `ticket_completed`, `ticket_failed`; future workflow transitions slot in without a migration.

### Key indexes

- `(user_id, read_at, created_at desc)` — the primary read path ("unread + recent for me").
- `(user_id, org_id, type, created_at desc)` — per-org / per-type filter combinations.

### Core flows

- **Record.** `record(...) -> Notification | None` — idempotent by `(user_id, type, ticket_id)`. Re-emitting the same workflow transition is a no-op (returns `None`).
- **Read.** `list_for_user(...) -> list[Notification]` filters by `read_state` (`all` / `unread` / `read`), optional `org_id`, optional `types`. `popover_for_user(...) -> tuple[list[Notification], int]` returns the latest N unread items + the unread count for the sidebar bell.
- **Mark read.** `mark_read(...) -> Notification | None` flips `read_at` to now if null (idempotent). `mark_all_read(org_id?, types?) -> int` does a single bulk UPDATE; returns the row count.
- **Task handler — `handle_ticket_status_change`.** Accepts `ticket_id`, `member_user_ids`, `org_id`, `new_status`. Looks up the ticket title (for the notification body), maps `new_status` to a `notif_type` via a fixed table, and calls `record(...)` for each supplied `user_id`. Producers compute the recipient list inside their own transaction — atomic with the status change — so the task body does no membership query. The `org_id` contextvar is set by `core/tasks` middleware before the body runs; the handler never calls `org_context` itself. Idempotent: `record` deduplicates on `(user_id, type, ticket_id)`.

### Task handler — the only write path

Ticket-status notifications flow exclusively through `enqueue(handle_ticket_status_change, ...)`. The `subscribers.py` file still exists but is no longer wired — `on_startup` was removed from the `RouteSpec`; no `core/events` subscriber runs. The task handler is the sole path for writing notification rows.

## Data owned

- `notifications` table (created by migration `021_create_notifications`).

## How it's tested

- `apps/backend/app/domain/notifications/test/test_endpoints.py` — service tests against a real DB session covering unauth 401, per-user scoping (Alice's list doesn't surface Bob's rows and vice versa), popover unread_count, `mark_read` idempotency, `mark_all_read` scoping, and `service.record()` idempotency by `(user_id, type, ticket_id)`.
- `apps/backend/app/domain/notifications/test/test_subscriber_service.py` — service tests for the legacy event-bus subscriber: status → notif_type mapping, per-member write, `running` filtered out, idempotent re-emission.
- `apps/backend/app/domain/notifications/test/test_task_handler_service.py` — service tests for the task handler: per-member write, idempotency on redelivery, and end-to-end durability via the outbox drain (enqueue → drain → task body → notification rows).
