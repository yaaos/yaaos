# domain/tickets

> yaaos's unit of work — owns `tickets`, the PR mirror (`pull_requests` table), the lifecycle state machine, ticket-status change publishing, and notification policy.

## Scope

Owns: ticket identity, status transitions, idempotent creation, SSE + durable-task publishing on every transition, notification policy for status changes (`notifications.py:build_status_change_specs`), findings rollup columns (`findings_count`, `max_severity`). Also owns the `pull_requests` table: `PullRequestRow`, `PullRequest` VO, `PRState`, and the five PR services (`upsert`, `update_state`, `get`, `get_by_external`, `list_by_ids`) — all in `tickets/pull_request.py`.

Does NOT own: pipeline-run state (`domain/pipelines`), finding state (`domain/findings`), workspace lifecycle, notification delivery (delegated to [core/notifications](core_notifications.md)). Does NOT aggregate findings at read time — `domain/findings` writes the rollup via `update_findings_summary`. The `GET /api/tickets/{id}/audit` endpoint aggregates ticket + PR audit entries only; finding audit entries are findings-owned and not included.

## Why / invariants

- **Two signals fire atomically with every transition commit:** `core/sse.publish_general_after_commit` (SPA) and `core/tasks.enqueue` (durable outbox). Rolled-back transactions emit neither.
- **Notification policy lives here, not in `core/notifications`.** `build_status_change_specs` decides which statuses generate notifications, which type to assign, and what title to show. It returns `list[NotificationSpec]`; the caller enqueues `core/notifications.fanout`. `plugins/github` uses the same helper — no plugin owns notification policy.
- **`(org_id, source, source_external_id)` UNIQUE** collapses concurrent webhook deliveries. `_insert_ticket_atomic` uses `INSERT … ON CONFLICT DO NOTHING … RETURNING`; on conflict the race loser re-SELECTs the winner's id so callers always get a non-None UUID and `created=False`.
- **`create_from_<source>` is the intake-constructor convention.** `create_from_pr` wraps `_insert_ticket_atomic` with fixed `type="github_pr"`, `source="github_pr"`, `status="pending"`. Optional `branch_name` (intake-supplied — a PR ticket's own head branch) rides straight into the INSERT; when omitted, `created=True` mints one via `mint_branch_name(title, ticket_id)` and `UPDATE`s it in (the ticket id is only known post-insert). On `created=True` writes `ticket.created` audit + `notify_ticket_status_change(None→pending)`; on `created=False` returns the existing id immediately so callers can exit (a race loser never touches `branch_name` — the winner already set it). `create_from_schedule` is the second instance — fixed `type="schedule"`, `source="schedule"`; always mints `branch_name` fresh (schedule tickets are yaaos-authored, no upstream branch to inherit); called by [`domain/pipelines`](domain_pipelines.md)'s `pipeline_schedule_tick` with `source_external_id="{binding_id}:{fire_time}"`. Both share the identical post-insert bookkeeping shape.
- **`notify_ticket_status_change` is the sole broadcast seam.** Fires `publish_general_after_commit` + `enqueue(fanout)` from one place so no caller can accidentally omit either signal.
- **`attach_pr_to_ticket` owns the `ticket.pr_bound` audit row.** The `WHERE pr_id IS NULL` guard makes it idempotent: concurrent calls produce at most one audit row.
- **Terminal states have no outbound transitions** for the unconditional helpers. `complete`/`fail`/`abandon` go through `_transition`, which raises `InvalidTicketTransition`. The guarded `transition_ticket_on_run_terminal` returns `False` instead of raising — safe inside a caller's transaction.
- **`_apply_transition` is the shared side-effect kernel.** `_transition` (Shape-b) and the `transition_ticket_on_run_*` family (Shape-a) all delegate to it. It delegates SSE + notification outbox to `notify_ticket_status_change` so the broadcast seam is used consistently.
- **`transition_ticket_on_run_start`/`transition_ticket_on_run_terminal` are called directly by the run engine** ([`domain/pipelines`](domain_pipelines.md) — a plain acyclic `pipelines → tickets` import, no hook indirection), keyed on `current_run_id`. Same guard semantics: silent `False` on ticket-not-found, wrong owner, or already-past-the-relevant-state; caller commits.
- **`transition_ticket_on_run_paused`/`transition_ticket_on_run_resumed`** flip a ticket `→ hitl` when its owning run's boundary evaluation pauses, and `hitl → running` when that pause resolves with `approve`. Same ownership guard as the terminal pair (`current_run_id` match, not already terminal for the pause direction; `status == "hitl"` for the resume direction) — a pause is not itself a terminal run state, so it needed its own entry points rather than overloading `transition_ticket_on_run_terminal`'s terminal-only contract.
- **Workspace ≠ ticket.** The run engine provisions one workspace per run; it is anonymous from the ticket's perspective — no FK, no column.
- **`findings_count` + `max_severity` are denormalized, not live-aggregated.** `domain/findings` writes them via `update_findings_summary` after each finding report or verdict. `list_tickets` reads them directly from the row — no cross-module import from tickets → findings.
- **All ticket reads are org-scoped.** Use `get(ticket_id, org_id=...)` — the unscoped `get_by_id` helper has been removed.
- Three `source` values are accepted today: `github_pr` (`create_from_pr`), `schedule` (`create_from_schedule`), and `manual` (`create_from_manual`). Manual tickets have `type="manual"`, `source="manual"`, empty `plugin_id`, and use a caller-supplied or auto-minted `idempotency_key` as `source_external_id`. With `idempotency_key=None` (the default) a fresh `uuid7()` is minted on every call, producing a distinct ticket each time. Callers that need replay-safety supply a stable `idempotency_key`.
- **`create_from_manual`** is Shape-a (caller's session, never commits). Returns `(ticket_id: UUID, created: bool)` — same as the other constructors.
- **`get_by_branch`** (Shape-a) returns the newest `Ticket` on `branch_name` within `org_id`, or `None`. Orders by `(created_at DESC, id DESC)` so tickets inserted in the same transaction resolve deterministically by UUIDv7 insertion order.
- **PR mirror invariants (from `pull_request.py`):** `upsert` never commits — the caller composes ticket + PR + audit atomically. `ticket_id` is required on insert, ignored on update. `list_by_ids` silently omits unknown ids and short-circuits on empty input. No state-machine validation on `update_state` — VCS is the source of truth. Immutable after insert: `plugin_id`, `external_id`, `number`, `repo_external_id`, `ticket_id`, `author_*`, `base_branch`, `head_branch`, `is_fork`.

## State machine

| From → To | Trigger |
|---|---|
| (none) → `pending` | `create_from_pr` (GitHub PR intake) |
| (none) → `pending` | `create_from_schedule` (schedule-kind trigger binding firing) |
| (none) → `pending` | `create_from_manual` (user-initiated kickoff via `POST /api/tickets`) |
| `pending` → `running` | `transition_ticket_on_run_start(...)` — called directly by the run engine when a `pipeline_runs` row is promoted to `running` |
| `cancelled` → `running` | `transition_ticket_on_run_start(...)` — also accepts `cancelled` source state (set when a kill+replace run-start kills the current run) |
| `running` → `done` | `complete` (PR closed/merged) |
| `running` → `cancelled` | `abandon(reason=...)` |
| `running` → `failed` | `fail(reason=...)` — orphan sweep (never-dispatched tickets only) |
| `pending`/`running` → `done`/`failed`/`cancelled` | `transition_ticket_on_run_terminal(...)` — called directly by the run engine at every `pipeline_runs` terminal |
| `running` → `hitl` | `transition_ticket_on_run_paused(...)` — called directly by the run engine's `_enter_pause` when a stage's boundary evaluation trips a pause |
| `hitl` → `running` | `transition_ticket_on_run_resumed(...)` — called directly by the run engine's `resume_from_pause` when a pause resolves `approve` |

`complete` / `abandon` / `fail` are Shape-b (own session) and go through `_transition`. The `transition_ticket_on_run_*` family is Shape-a (caller's session, never commits) and is called directly by the run engine.

## Data owned

`tickets` — canonical schema in [core_database.md](core_database.md). Includes `findings_count INT NOT NULL DEFAULT 0` and `max_severity VARCHAR NULL` — written by `domain/findings`, read by this module. Also carries `current_run_id UUID NULL` (soft ref to `pipeline_runs`, [domain_pipelines.md](domain_pipelines.md); written by `set_current_run` when the run engine promotes a run to `running`, read by `transition_ticket_on_run_start`/`transition_ticket_on_run_terminal` as the ownership guard) and `branch_name VARCHAR NULL` (per-ticket work branch; every `create_from_pr`/`create_from_schedule` ticket gets one — the PR's own head branch, or a minted fallback; exposed on the `Ticket` VO and read by the run engine's action-stage dispatch). `mint_branch_name(title, ticket_id)` is the pure minting function (`yaaos/<slug>-<ticket_id.hex[:8]>`, falling back to `yaaos/ticket-<...>` when the title yields no slug), called by `create_from_pr` when the caller doesn't already know the branch, and always by `create_from_schedule` (schedule tickets never have an upstream branch to inherit).

`pull_requests` — `(id, org_id, plugin_id, external_id, …)`. Unique on `(plugin_id, external_id)`. FK `ticket_id → tickets.id`. Implemented in `tickets/pull_request.py`; table name unchanged from previous module location.

## HTTP routes

`RouteSpec.url_prefix` resolves to `/api/tickets`.

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/tickets` | `REVIEWER_WRITE` | Create a manual ticket. Body: `{title, repo_external_id, branch_name?, idempotency_key?}`. Returns `{id, created}`. 201 on success. |
| `GET` | `/api/tickets` | `REVIEWER_READ` | List tickets. Query params: `status?`, `author?`, `q?`, `cursor?`, `branch_name?`. |
| `GET` | `/api/tickets/dashboard` | `REVIEWER_READ` | Dashboard projection (stats + in_flight + needs_attention). |
| `GET` | `/api/tickets/{ticket_id}` | `REVIEWER_READ` | Ticket detail including enriched PR fields and builder info. |
| `GET` | `/api/tickets/{ticket_id}/audit` | `REVIEWER_READ` | Ticket + PR audit entries for the given ticket. |

## How it's tested

- `test/test_service.py` — `create_from_pr` (create + idempotent race-loser re-SELECT), `attach_pr_to_ticket`, `list_tickets` reads row-backed rollup + DB sort.
- `test/test_create_from_pr_idempotent_service.py` (`@pytest.mark.service`) — concurrent `create_from_pr` calls produce exactly one TicketRow; race loser returns winner's id.
- `test/test_attach_pr_to_ticket_idempotent_service.py` (`@pytest.mark.service`) — concurrent `attach_pr_to_ticket` calls produce at most one `ticket.pr_bound` audit row.
- `test/test_status_change_producer_service.py` — `notifications.fanout` outbox row; exactly two SSE events for create+start (None→pending, pending→done) with no duplicates; no SSE on rollback.
- `create_from_schedule`'s redelivery-idempotency + branch-minting are exercised end-to-end by [`domain/pipelines/test/test_schedule_tick_service.py`](domain_pipelines.md#how-its-tested) via the consuming `pipeline_schedule_tick`.
- `test/test_pr_upsert_session.py` — session-ownership (insert + update, FK safety, missing ticket_id guard).
- `test/test_pull_request_service.py` (`@pytest.mark.service`) — `list_by_ids`: full match, empty input, unknown ids, partial match.

See [core_notifications.md](core_notifications.md), [core_sse.md](core_sse.md), [core_tasks.md](core_tasks.md), [domain_pipelines.md](domain_pipelines.md), [core_coding_agent.md](core_coding_agent.md).
