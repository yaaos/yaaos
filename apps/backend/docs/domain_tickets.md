# domain/tickets

> yaaos's unit of work — owns `tickets`, the lifecycle state machine, and ticket-status change publishing.

## Scope

Owns: ticket identity, status transitions, idempotent creation, SSE + durable-task publishing on every transition.

Does NOT own: PR mirror state (`pull_requests`), review state (`reviewer`), workspace lifecycle, audit entries beyond `ticket.created` / `ticket.status_changed`.

## Why / invariants

- **Two signals fire atomically with every transition commit:** `core/sse.publish_general_after_commit` (SPA) and `core/tasks.enqueue` (durable outbox). Rolled-back transactions emit neither.
- **`(org_id, source, source_external_id)` UNIQUE** collapses concurrent webhook deliveries. `upsert_ticket_for_pr` uses `INSERT … ON CONFLICT DO NOTHING`; the race loser gets `(None, False)` and exits.
- **Terminal states have no outbound transitions.** Enforced in code (`_transition` raises `InvalidTicketTransition`), not a DB CHECK.
- **Workspace ≠ ticket.** The reviewer opens one workspace per coordinator call; it is anonymous from the ticket's perspective — no FK, no column.
- `source != "github_pr"` is not accepted today; future sources need new validation.

## State machine

| From → To | Trigger |
|---|---|
| (none) → `pending` | `create` (generic intake) |
| (none) → `running` | `create_for_pr` / `upsert_ticket_for_pr` |
| `pending` → `running` | workflow-step dispatch |
| `running` → `done` | `complete` (PR closed/merged) |
| `running` → `cancelled` | `abandon(reason=...)` |
| `running` → `failed` | `fail(reason=...)` — orphan sweep, future workflow failures |

`complete` / `abandon` / `fail` all go through `_transition` in `service.py`.

## Data owned

`tickets` — canonical schema in [core_database.md](core_database.md).

## How it's tested

- `test/test_service.py` — `upsert_ticket_for_pr` (create + race-loser), `attach_pr_to_ticket`, `set_workflow_execution`.
- `test/test_status_change_producer_service.py` — outbox row, SSE after commit, no SSE on rollback.
- `test/test_workspace_ticket_context.py` — `get_workspace_ticket_context` read path.

See [domain_notifications.md](domain_notifications.md), [core_sse.md](core_sse.md), [core_tasks.md](core_tasks.md).
