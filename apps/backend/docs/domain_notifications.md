# domain/notifications

> Cross-org user inbox — one row per user-targeted event; the SPA renders unread items in the sidebar bell and `/notifications` page.

## Scope

Owns: `notifications` table, read/write API. All notification writes flow through a single durable task handler — no direct inserts from other modules.

## Why / invariants

- **Idempotent writes on `(user_id, type, ticket_id)`.** Re-emitting the same workflow transition is a no-op; `record` returns `None`.
- **Task handler, not direct call.** `handle_ticket_status_change` is a `TaskRef`; producers `enqueue` it. The task handler is the sole write path for notification rows.
- **Recipients pre-computed in the producer's transaction.** The task body receives `member_user_ids` and does no membership query — avoiding a cross-transaction race.
- **`USER_SCOPED` prefix** — middleware does not demand `X-Org-Slug`; handlers resolve the session cookie directly (same as `/api/orgs/mine`).
- `type` is freeform text; future workflow transitions slot in without a migration.

## Data owned

`notifications` — `(user_id, org_id, type, ticket_id?, title, body, read_at?, created_at)`. Created by migration `021_create_notifications`.

## How it's tested

- `test/test_endpoints.py` — 401, per-user scoping, popover unread_count, `mark_read` idempotency, `mark_all_read` scoping, `record` idempotency.
- `test/test_task_handler_service.py` — per-member write, redelivery idempotency, outbox drain end-to-end.
