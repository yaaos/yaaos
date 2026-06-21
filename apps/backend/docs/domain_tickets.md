# domain/tickets

> yaaos's unit of work — owns `tickets`, the PR mirror (`pull_requests` table), the lifecycle state machine, ticket-status change publishing, and notification policy.

## Scope

Owns: ticket identity, status transitions, idempotent creation, SSE + durable-task publishing on every transition, notification policy for status changes (`notifications.py:build_status_change_specs`), findings rollup columns (`findings_count`, `max_severity`), the Ticket page's workflow-run + step-activity read endpoints. Also owns the `pull_requests` table: `PullRequestRow`, `PullRequest` VO, `PRState`, and the five PR services (`upsert`, `update_state`, `get`, `get_by_external`, `list_by_ids`) — all in `tickets/pull_request.py`.

Does NOT own: review state (`reviewer`), workspace lifecycle, notification delivery (delegated to [core/notifications](core_notifications.md)). Does NOT aggregate findings at read time — `reviewer` writes the rollup via `update_findings_summary`. The `GET /api/tickets/{id}/audit` endpoint aggregates ticket + PR audit entries only; review_job and finding audit entries are reviewer-owned and not included.

## Why / invariants

- **Two signals fire atomically with every transition commit:** `core/sse.publish_general_after_commit` (SPA) and `core/tasks.enqueue` (durable outbox). Rolled-back transactions emit neither.
- **Notification policy lives here, not in `core/notifications`.** `build_status_change_specs` decides which statuses generate notifications, which type to assign, and what title to show. It returns `list[NotificationSpec]`; the caller enqueues `core/notifications.fanout`. `plugins/github` uses the same helper — no plugin owns notification policy.
- **`(org_id, source, source_external_id)` UNIQUE** collapses concurrent webhook deliveries. `_insert_ticket_atomic` uses `INSERT … ON CONFLICT DO NOTHING … RETURNING`; on conflict the race loser re-SELECTs the winner's id so callers always get a non-None UUID and `created=False`.
- **`create_from_pr` is the single intake constructor.** Wraps `_insert_ticket_atomic` with fixed `type="github_pr"`, `source="github_pr"`, `status="pending"`. On `created=True` writes `ticket.created` audit + `notify_ticket_status_change(None→pending)`; on `created=False` returns the existing id immediately so callers can exit.
- **`notify_ticket_status_change` is the sole broadcast seam.** Fires `publish_general_after_commit` + `enqueue(fanout)` from one place so no caller can accidentally omit either signal.
- **`attach_pr_to_ticket` owns the `ticket.pr_bound` audit row.** The `WHERE pr_id IS NULL` guard makes it idempotent: concurrent calls produce at most one audit row.
- **Terminal states have no outbound transitions** for the unconditional helpers. `complete`/`fail`/`abandon` go through `_transition`, which raises `InvalidTicketTransition`. The guarded `transition_on_workflow_terminal` returns `False` instead of raising — safe inside a caller's transaction.
- **`_apply_transition` is the shared side-effect kernel.** `_transition` (Shape-b), `transition_on_workflow_terminal` (Shape-a), and `transition_on_workflow_start` (Shape-a) all delegate to it. It delegates SSE + notification outbox to `notify_ticket_status_change` so the broadcast seam is used consistently.
- **`transition_on_workflow_start` flips `pending → running` atomically with the workflow bootstrap commit.** Called via `transition_ticket_on_start` (in `workflow_callbacks.py`) which is wired into `pr_review_v1.on_start` — the engine awaits it inside the bootstrap-commit transaction. Shape-a (caller's session, never commits). Guards: ticket not found, wrong org, different `current_workflow_execution_id`, or already past `pending` → returns `False` silently. Returns `True` when flipped.
- **`on_terminal(terminal_state=CANCELLED)` maps to ticket status `cancelled`** via `_STATE_TO_STATUS` in `workflow_callbacks.py`. The engine fires `on_terminal` with `terminal_state=WorkflowState.CANCELLED, failure_reason=None` on cancel; `transition_ticket_on_terminal` translates to `to_status="cancelled"` atomically with the engine's terminal commit.
- **Workspace ≠ ticket.** The reviewer opens one workspace per coordinator call; it is anonymous from the ticket's perspective — no FK, no column.
- **`findings_count` + `max_severity` are denormalized, not live-aggregated.** Reviewer writes them via `update_findings_summary` after each review run and on ack/push-back. `list_tickets` reads them directly from the row — no cross-module import from tickets → reviewer.
- **All ticket reads are org-scoped.** Use `get(ticket_id, org_id=...)` — the unscoped `get_by_id` helper has been removed.
- `source != "github_pr"` is not accepted today; future sources need new validation.
- **PR mirror invariants (from `pull_request.py`):** `upsert` never commits — the caller composes ticket + PR + audit atomically. `ticket_id` is required on insert, ignored on update. `list_by_ids` silently omits unknown ids and short-circuits on empty input. No state-machine validation on `update_state` — VCS is the source of truth. Immutable after insert: `plugin_id`, `external_id`, `number`, `repo_external_id`, `ticket_id`, `author_*`, `base_branch`, `head_branch`, `is_fork`.

## State machine

| From → To | Trigger |
|---|---|
| (none) → `pending` | `create_from_pr` (GitHub PR intake) |
| `pending` → `running` | `transition_on_workflow_start(...)` — via `pr_review_v1.on_start` callback, atomic with bootstrap RUNNING write |
| `running` → `done` | `complete` (PR closed/merged) |
| `running` → `cancelled` | `abandon(reason=...)` |
| `running` → `failed` | `fail(reason=...)` — orphan sweep (never-dispatched tickets only) |
| `pending`/`running` → `done`/`failed`/`cancelled` | `transition_on_workflow_terminal(...)` — via `pr_review_v1.on_terminal` callback (primary path off `running`) |

`complete` / `abandon` / `fail` are Shape-b (own session) and go through `_transition`. `transition_on_workflow_terminal` and `transition_on_workflow_start` are Shape-a (caller's session, never commits) and are called from `workflow_callbacks.py`.

## Data owned

`tickets` — canonical schema in [core_database.md](core_database.md). Includes `findings_count INT NOT NULL DEFAULT 0` and `max_severity VARCHAR NULL` — written by reviewer, read by this module.

`pull_requests` — `(id, org_id, plugin_id, external_id, …)`. Unique on `(plugin_id, external_id)`. FK `ticket_id → tickets.id`. Implemented in `tickets/pull_request.py`; table name unchanged from previous module location.

## How it's tested

- `test/test_service.py` — `create_from_pr` (create + idempotent race-loser re-SELECT), `attach_pr_to_ticket`, `set_workflow_execution`, `list_tickets` reads row-backed rollup + DB sort.
- `test/test_create_from_pr_idempotent_service.py` (`@pytest.mark.service`) — concurrent `create_from_pr` calls produce exactly one TicketRow; race loser returns winner's id.
- `test/test_attach_pr_to_ticket_idempotent_service.py` (`@pytest.mark.service`) — concurrent `attach_pr_to_ticket` calls produce at most one `ticket.pr_bound` audit row.
- `test/test_status_change_producer_service.py` — `notifications.fanout` outbox row; exactly two SSE events for create+start (None→pending, pending→done) with no duplicates; no SSE on rollback.
- `test/test_transition_on_workflow_terminal_service.py` (`@pytest.mark.service`) — all guard branches for `transition_on_workflow_terminal`: owner + running → flips + audit row + returns True; non-owner → no-op + False; already terminal → idempotent + False; missing ticket / wrong org → False + no raise; no commit inside fn.
- `test/test_transition_on_workflow_start_service.py` (`@pytest.mark.service`) — all guard branches for `transition_on_workflow_start`: pending + matching wfx_id → flips to running + audit row + True; wrong wfx_id → False; already running → False; missing ticket → False; no commit inside fn.
- `test/test_workspace_ticket_context.py` — `get_workspace_ticket_context` read path.
- `test/test_pr_upsert_session.py` — session-ownership (insert + update, FK safety, missing ticket_id guard).
- `test/test_pull_request_service.py` (`@pytest.mark.service`) — `list_by_ids`: full match, empty input, unknown ids, partial match.

## Workflow-run read surface

Two GET routes back the Ticket page's workflow view (see `apps/backend/app/domain/tickets/web.py`):

- `GET /api/tickets/{ticket_id}/workflow-runs` — projects every workflow execution attached to the ticket via [`core/workflow.list_run_views_for_ticket`](core_workflow.md), oldest first. Each run carries `{id, workflow_name, workflow_version, state, current_step_id, failure_reason, created_at, updated_at, steps[]}`; each `step` carries `{step_id, command_kind, state, started_at, completed_at}`. 404 when the ticket is missing.
- `GET /api/tickets/{ticket_id}/activity/{execution_id}/{step_id}` — returns `{activity: <log> | null}` via [`core/coding_agent.get_step_activity`](core_coding_agent.md). 404 when the `workflow_executions` row doesn't belong to the ticket — cross-tenant safe by construction. `null` when the partition has aged out (>4 weeks).

The SPA invalidates the run-view query on every `workflow_state_changed` SSE event from [`core/sse`](core_sse.md).

See [core_notifications.md](core_notifications.md), [core_sse.md](core_sse.md), [core_tasks.md](core_tasks.md), [core_workflow.md](core_workflow.md), [core_coding_agent.md](core_coding_agent.md).
