# `domain/tickets` — Internal Architecture

> yaaof's unit of work. Owns the `tickets` table, the lifecycle state machine, ticket creation from a PR, ticket list/detail queries, and `TicketStatusChanged` event publishing.

## Purpose

`domain/tickets` is the home of the ticket aggregate. In M01:

- Every ticket has `source='github_pr'` and is linked to one PR.
- The state machine is trivial: ticket is born `in_review`, transitions to `complete` when the PR closes/merges, or `abandoned` if the repo is removed mid-flight (or by force from an admin).
- The `'open'` state is reserved for M02+ ticket sources (Linear/Jira/Slack) that exist before any review starts.

## Public interface (`__all__`)

```python
# Types
"Ticket",                # the aggregate root
"TicketFilter",          # list filter
"TicketStatusChanged",   # event published on every transition

# Functions
"create_for_pr",         # create a new ticket linked to a PR
"get",                   # by id
"get_by_pr",             # by linked PR id
"list_tickets",          # paginated filter+search

# Transitions
"complete",              # in_review → complete
"abandon",               # in_review → abandoned

# Exceptions
"TicketNotFoundError",
"InvalidTicketTransition",
```

## `Ticket` model

Pydantic representation of a row, returned by query functions:

```python
class Ticket(BaseModel):
    id: UUID
    org_id: UUID
    source: Literal["github_pr"]   # extends in M02+
    source_external_id: str
    title: str
    description: str | None
    status: TicketStatus            # "open" | "in_review" | "complete" | "abandoned"
    repo_id: UUID
    pr_id: UUID | None              # always set in M01
    created_at: datetime
    updated_at: datetime
```

## `TicketFilter`

```python
class TicketFilter(BaseModel):
    repo_ids: list[UUID] | None = None
    author_logins: list[str] | None = None   # matches pull_requests.author_login of the linked PR
    created_after: datetime | None = None
    created_before: datetime | None = None
    statuses: list[TicketStatus] | None = None
```

All fields optional. Empty filter (or fields all None) matches everything (subject to `org_id`).

## `TicketStatusChanged` event

```python
class TicketStatusChanged(Event):
    kind: Literal["ticket_status_changed"] = "ticket_status_changed"
    source_module: Literal["tickets"] = "tickets"
    ticket_id: UUID
    repo_id: UUID
    pr_id: UUID | None
    previous_status: TicketStatus
    new_status: TicketStatus
    reason: str | None       # populated for abandon (e.g., "repo_removed")
```

Subclasses `core.events.Event`. Published via `events.publish(...)` on every status transition.

## Functions

### `create_for_pr`

```python
async def create_for_pr(
    repo_id: UUID,
    pr: PullRequest,         # the upserted yaaof PR record
    *,
    org_id: UUID,
) -> Ticket:
    """Create a new ticket for the given PR, status='in_review'.
    Idempotent: if a ticket already exists for this pr_id, return it (no error)."""
```

Called by `intake.refresh_pr_metadata` when it upserts a PR that has no existing ticket. Writes `audit_for_ticket(kind='ticket.created', payload={pr_id, repo_id}, actor=Actor(system))`.

After commit, publishes `TicketStatusChanged(previous=None, new='in_review')`.

### `get` / `get_by_pr`

```python
async def get(ticket_id: UUID, *, org_id: UUID) -> Ticket:
    """Raises TicketNotFoundError if not found."""

async def get_by_pr(pr_id: UUID, *, org_id: UUID) -> Ticket | None:
    """Returns the ticket linked to this PR, or None."""
```

### `list_tickets`

```python
async def list_tickets(
    filter: TicketFilter,
    *,
    limit: int = 50,
    before_ts: datetime | None = None,
    org_id: UUID,
) -> list[Ticket]:
    """Returns matching tickets, ordered by updated_at DESC. Cursor pagination via before_ts."""
```

Used by the ticket list UI. The query joins to `pull_requests` only when `author_logins` is set (otherwise no join needed). Indexes on `(org_id, updated_at)` cover the common path.

### Transitions

```python
async def complete(ticket_id: UUID, *, org_id: UUID) -> None:
    """in_review → complete. Called by intake when a PR closes/merges.
    Raises InvalidTicketTransition if current status is not in_review."""

async def abandon(ticket_id: UUID, *, reason: str, org_id: UUID) -> None:
    """Any status (except complete/abandoned) → abandoned.
    Called when a repo is removed from allowlist or by force.
    `reason` is captured in the audit + event payload."""
```

Each transition:

1. Loads the ticket row with `SELECT ... FOR UPDATE` inside the caller's transaction.
2. Validates the transition (raises `InvalidTicketTransition` if invalid).
3. Updates `status` + `updated_at`.
4. Writes `audit_for_ticket(kind='ticket.status_changed', payload={from, to, reason?}, actor=...)`.
5. **After** the caller's transaction commits, publishes `TicketStatusChanged` on `core/events`. The publish happens via a post-commit hook (SQLAlchemy event listener or an `after_commit` callback in the service layer) — never inside the transaction, so subscribers see committed state.

## State machine

| Current → New | Allowed | Trigger |
|---|---|---|
| (none) → `in_review` | ✓ | `create_for_pr` |
| `in_review` → `complete` | ✓ | `complete` |
| `in_review` → `abandoned` | ✓ | `abandon` (e.g., repo removed) |
| `open` → `in_review` | ✓ | (M02+ only — when a coding agent kicks off a review on a previously-open ticket) |
| `open` → `abandoned` | ✓ | (M02+) |
| `complete` → * | ✗ | terminal |
| `abandoned` → * | ✗ | terminal |

M01 only exercises the first three transitions.

The state machine is enforced at the function level (in `complete` and `abandon`), not via DB CHECK constraints. Easier to evolve when M02+ adds more transitions.

## Caller responsibility

`tickets` doesn't decide *when* to transition — callers do:

- `intake.handle_pr_closed` → `tickets.complete(ticket_id)`
- `intake.handle_repo_removed` (future) or `repos.remove(repo_id)` → fetches affected tickets, calls `tickets.abandon(ticket_id, reason='repo_removed')`
- An admin UI button (future) could call `abandon(reason='admin_action')`

## What `domain/tickets` does NOT do

- Does not own PR mirror state (`pull_requests` does).
- Does not own review state (`reviewer` does).
- Does not write to `audit_log` for non-transition events. Only `ticket.created` and `ticket.status_changed`.
- Does not subscribe to its own events.
- Does not enforce M02+ source-specific rules (it accepts only `github_pr` for now; raises `ValueError` on other sources to fail fast).

## Decisions

### 2026-05-14 — `TicketFilter` is a Pydantic model
Typed; FastAPI builds it from query params; easy to extend without breaking call sites.

### 2026-05-14 — Per-transition functions, not a generic `transition(to=...)`
`complete(ticket_id)` and `abandon(ticket_id, reason=...)` are explicit. Grep-able. Each function validates its own preconditions.

### 2026-05-14 — Transitions publish `TicketStatusChanged` after commit
SQLAlchemy `after_commit` hook (or equivalent). Subscribers see post-commit state; no read-your-writes inconsistency for the UI.

### 2026-05-14 — State machine enforced in code, not via DB CHECK
Easier to evolve when M02+ adds new transitions. The DB stores the value; the service layer validates.

### 2026-05-14 — `create_for_pr` is idempotent
Calling twice for the same `pr_id` returns the existing ticket. Lets intake call it from every PR webhook without checking existence first.
