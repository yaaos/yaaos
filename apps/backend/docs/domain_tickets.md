# domain/tickets

> yaaos's unit of work — owns `tickets`, the lifecycle state machine, ticket-status change publishing, and notification policy.

## Scope

Owns: ticket identity, status transitions, idempotent creation, SSE + durable-task publishing on every transition, notification policy for status changes (`notifications.py:build_status_change_specs`), findings rollup columns (`findings_count`, `max_severity`), the Ticket page's workflow-run + step-activity read endpoints.

Does NOT own: PR mirror state (`pull_requests`), review state (`reviewer`), workspace lifecycle, notification delivery (delegated to [core/notifications](core_notifications.md)). Does NOT aggregate findings at read time — `reviewer` writes the rollup via `update_findings_summary`. The `GET /api/tickets/{id}/audit` endpoint aggregates ticket + PR audit entries only; review_job and finding audit entries are reviewer-owned and not included.

## Why / invariants

- **Two signals fire atomically with every transition commit:** `core/sse.publish_general_after_commit` (SPA) and `core/tasks.enqueue` (durable outbox). Rolled-back transactions emit neither.
- **Notification policy lives here, not in `core/notifications`.** `build_status_change_specs` decides which statuses generate notifications, which type to assign, and what title to show. It returns `list[NotificationSpec]`; the caller enqueues `core/notifications.fanout`. `plugins/github` uses the same helper — no plugin owns notification policy.
- **`(org_id, source, source_external_id)` UNIQUE** collapses concurrent webhook deliveries. `upsert_ticket_for_pr` uses `INSERT … ON CONFLICT DO NOTHING`; the race loser gets `(None, False)` and exits.
- **Terminal states have no outbound transitions.** Enforced in code (`_transition` raises `InvalidTicketTransition`), not a DB CHECK.
- **Workspace ≠ ticket.** The reviewer opens one workspace per coordinator call; it is anonymous from the ticket's perspective — no FK, no column.
- **`findings_count` + `max_severity` are denormalized, not live-aggregated.** Reviewer writes them via `update_findings_summary` after each review run and on ack/push-back. `list_tickets` reads them directly from the row — no cross-module import from tickets → reviewer.
- **All ticket reads are org-scoped.** Use `get(ticket_id, org_id=...)` — the unscoped `get_by_id` helper has been removed.
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

`tickets` — canonical schema in [core_database.md](core_database.md). Includes `findings_count INT NOT NULL DEFAULT 0` and `max_severity VARCHAR NULL` — written by reviewer, read by this module.

## How it's tested

- `test/test_service.py` — `upsert_ticket_for_pr` (create + race-loser), `attach_pr_to_ticket`, `set_workflow_execution`, `list_tickets` reads row-backed rollup + DB sort.
- `test/test_status_change_producer_service.py` — `notifications.fanout` outbox row, SSE after commit, no SSE on rollback.
- `test/test_workspace_ticket_context.py` — `get_workspace_ticket_context` read path.

## Workflow-run read surface

Two GET routes back the Ticket page's workflow view (see `apps/backend/app/domain/tickets/web.py`):

- `GET /api/tickets/{ticket_id}/workflow-runs` — projects every workflow execution attached to the ticket via [`core/workflow.list_run_views_for_ticket`](core_workflow.md), oldest first. Each run carries `{id, workflow_name, workflow_version, state, current_step_id, failure_reason, created_at, updated_at, steps[]}`; each `step` carries `{step_id, command_kind, state, started_at, completed_at}`. 404 when the ticket is missing.
- `GET /api/tickets/{ticket_id}/activity/{execution_id}/{step_id}` — returns `{activity: <log> | null}` via [`domain/coding_agent.get_step_activity`](domain_coding_agent.md). 404 when the `workflow_executions` row doesn't belong to the ticket — cross-tenant safe by construction. `null` when the partition has aged out (>4 weeks).

The SPA invalidates the run-view query on every `workflow_state_changed` SSE event from [`core/sse`](core_sse.md).

See [core_notifications.md](core_notifications.md), [core_sse.md](core_sse.md), [core_tasks.md](core_tasks.md), [core_workflow.md](core_workflow.md), [domain_coding_agent.md](domain_coding_agent.md).
