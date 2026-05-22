# domain/tickets

> yaaos's unit of work — owns `tickets`, the lifecycle state machine, queries, and `TicketStatusChanged` event publishing.

## Purpose

Home of the ticket aggregate. Every ticket is born `in_review` when a PR is first observed, transitions to `complete` on close/merge, or `abandoned` if a caller forces it. `open` is reserved for future ticket sources that exist before review starts; today only `github_pr` is accepted. Owns no review state, no PR mirror state, and no audit-log writes outside its own transitions.

## Public interface

Exported from `app/domain/tickets/__init__.py`:

- `Ticket` — Pydantic row view; carries denormalized PR fields (`pr_number`, `author_login`, `is_draft`) populated at read time.
- `TicketFilter` — list filter (`repo_external_ids`, `author_logins`, `created_after`, `created_before`, `statuses`).
- `TicketStatus` — `Literal["open", "in_review", "complete", "abandoned"]` (legacy values; M05 also writes `pending|running|done|failed|cancelled` for intake-created tickets).
- `TicketStatusChanged` — published on every transition (subclass of `core.events.Event`).
- `TicketRow` — SQLAlchemy model (exported so cross-module joins avoid import cycles).
- Service — `create` (M05 intake-driven; idempotent on `idempotency_key`), `create_for_pr`, `get`, `get_by_pr`, `list_tickets`, `complete`, `abandon`, `attach_workflow_execution`.
- Exceptions — `TicketNotFoundError`, `InvalidTicketTransition`.

M05 columns on `tickets`: `type` (`pr_review` default), `idempotency_key` (sparse-unique), `payload` (JSONB), `current_workflow_execution_id` (soft pointer into `workflow_executions`). Created by migration `016_tickets_m05_columns`.

HTTP routes (`/api/tickets`):

- `GET /api/tickets` — list, with `repo_external_id[]` / `status[]` / `limit`.
- `GET /api/tickets/{ticket_id}` — detail.
- `GET /api/tickets/{ticket_id}/audit` — aggregated timeline (ticket + linked PR + every review_job's audit entries, newest first).

## Module architecture

### Files

- `models.py` — `TicketRow`.
- `service.py` — `Ticket`, `TicketFilter`, `TicketStatusChanged`, service functions, `_transition` (shared body of `complete`/`abandon`).
- `web.py` — FastAPI routes and `register_routes`.
- `module.py` — `get_module_name() -> "tickets"`.

### State machine

| Current → New | Trigger |
|---|---|
| (none) → `in_review` | `create_for_pr` |
| `in_review` → `complete` | `complete` (intake on PR close/merge) |
| `in_review` → `abandoned` | `abandon(reason=...)` |

`complete` and `abandon` share `_transition`: loads the row, refuses if terminal (raising `InvalidTicketTransition`), updates `status`, commits, writes the audit entry, and publishes `TicketStatusChanged` *after* commit. Terminal states have no outbound transitions. Enforced in code, not DB CHECK; column is plain `String`.

### Idempotent creation

`create_for_pr` is the only insert path. If a row exists for the given `pr_id`, it updates `title` / `description` and returns the existing ticket — callers can invoke unconditionally. Fresh insert writes `ticket.created` and publishes `TicketStatusChanged(previous=None, new='in_review')`.

In production the create path is reached *through* `intake.refresh_pr_metadata`, which creates the `TicketRow` directly so it can set `pull_requests.ticket_id` before back-filling `tickets.pr_id`. `create_for_pr` exists for direct callers and tests.

### Read-time denormalization

`get` and `list_tickets` enrich each ticket with `pr_number`, `author_login`, `is_draft` by joining to `pull_requests`. `list_tickets` batches PR lookups into one `WHERE id IN (...)` query.

### Event publishing

Every transition (including creation) publishes `TicketStatusChanged` with `previous_status`, `new_status`, optional `reason`. Publish happens after commit so subscribers see committed state. Canonical signal for downstream consumers.

### Aggregated audit timeline

`GET /api/tickets/{ticket_id}/audit` powers the detail UI's timeline tab. Pulls audit entries for ticket, linked PR, and every review_job for that PR; sorts newest-first. `reviewer.list_review_jobs_for_pr` is imported lazily to avoid a cycle.

### Caller responsibility

`tickets` does not decide *when* to transition. Current callers:

- `intake._handle_pr_closed` → `tickets.complete(ticket_id)` on merge or close.
- A future repo-removal flow → `tickets.abandon(ticket_id, reason='repo_removed')`.
- The audit endpoint reads but never writes.

### Relationship to workspaces

The "one workspace per ticket" principle is enforced by **runtime scope**, not by ticket data. Each review batch's coordinator (`_run_ticket_review` in `domain/reviewer`) opens one `core/workspace.with_workspace(...)` per coordinator call and gathers every agent against it; the workspace is destroyed when the last agent returns. There is no `tickets.workspace_id` column and no `workspaces.ticket_id` FK — workspaces are anonymous from the ticket's point of view.

The ticket aggregate does not own, expose, or coordinate workspace lifecycle. It owns identity and lifecycle state only.

This may change when a second workspace consumer lands (M02+ implementer agents would share workspaces across rounds on the same ticket — at which point `domain/tickets` is the natural home for a `with_ticket_workspace(ticket_id)` helper and a persistent linkage). Until then the runtime scoping is sufficient and keeps the ticket schema clean.

### What the module does not do

- Doesn't own PR mirror state (`pull_requests`) or review state (`domain/reviewer`).
- Doesn't own workspace lifecycle (see above).
- Doesn't subscribe to its own events.
- Doesn't write audit entries beyond `ticket.created` and `ticket.status_changed`.
- Doesn't accept `source != "github_pr"` — future sources require new validation.

## Data owned

- `tickets` — `(id, org_id, source, source_external_id, title, description, status, plugin_id, repo_external_id, pr_id, created_at, updated_at)`. Canonical schema in [core_database.md](core_database.md).

## How it's tested

`app/domain/tickets/test/` is currently `__init__.py` only; behaviour is exercised end-to-end by integration suites in `app/test/` and e2e in `apps/e2e/`. State-machine and event-publish semantics covered by intake and reviewer integration tests.
