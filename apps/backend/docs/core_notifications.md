# core/notifications

> Generic cross-org user inbox — one row per user-targeted event; the SPA renders unread items in the sidebar bell and `/notifications` page.

## Scope

- Owns: `notifications` table, read/write API.
- Does NOT own: domain knowledge about what triggered a notification (tickets, status maps, etc.) — producers supply fully-formed `NotificationSpec` values.
- Boundary: receives a list of `NotificationSpec` dicts via `fanout`; emits `Notification` VOs; hands to callers via `service.create` / HTTP responses.

## Why / invariants

- **Idempotent writes on `(user_id, type, subject_type, subject_id)`.** Re-emitting the same event for the same subject is a no-op; `create` returns `None`. Subject-less notifications (both null) bypass dedup and are always written.
- **`subject_type` and `subject_id` must be both null or both set.** `service.create` enforces this; violation raises `ValueError`.
- **`fanout` task, not direct call.** Producers `enqueue(fanout, ...)`. The task body opens its own session, calls `create` per spec, commits. No domain knowledge lives here.
- **Recipients pre-computed in the producer's transaction.** The task body does no membership or entity query — avoiding a cross-transaction race.
- **`USER_SCOPED` prefix** — middleware does not demand `X-Org-Slug`; handlers resolve the session cookie directly (same as `/api/orgs/mine`).
- `type` is freeform text; future event kinds slot in without a migration.

## Data owned

`notifications` — `(user_id, org_id, type, subject_type?, subject_id?, title, body, read_at?, created_at)`.

- Column `subject_id` (UUID, nullable) is the renamed former `ticket_id`; `subject_type` (VARCHAR(64), nullable) identifies the entity kind (e.g. `'ticket'`).
- Migration `031_notifications_generalize_subject` backfills `subject_type='ticket'` for pre-existing rows where `subject_id IS NOT NULL`.
- Dedup partial index `notifications_dedup_subject_idx` covers `(user_id, type, subject_type, subject_id)` where `subject_type IS NOT NULL`.

## How it's tested

- `test/test_endpoints.py` — 401, per-user scoping, popover `unread_count`, `mark_read` idempotency, `mark_all_read` scoping, `create` subject dedup.
- `test/test_task_handler_service.py` — `fanout` per-spec write, redelivery idempotency, outbox drain end-to-end, subject-pair invariant enforcement, dedup on subject tuple.

## Entry points

- `apps/backend/app/core/notifications/service.py` — `create`, `list_for_user`, `popover_for_user`, `mark_read`, `mark_all_read`.
- `apps/backend/app/core/notifications/tasks.py` — `NotificationSpec`, `fanout`.
- `apps/backend/app/core/notifications/web.py` — HTTP routes.
- `apps/backend/app/core/notifications/models.py` — `NotificationRow`.
