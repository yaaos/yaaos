# `core/audit_log` — Internal Architecture

> Append-only timeline primitive. Every meaningful action lands here.
> Domain modules write; the UI reads.

## Purpose

`core/audit_log` owns the `audit_entries` table (see [../data-model.md](../data-model.md)) and exposes:

- Per-entity write helpers (`audit_for_ticket`, `audit_for_pr`, etc.).
- A timeline read API (`list_for_entity`).
- A single-row read (`get`).

It has no business logic. It records facts; it doesn't decide what they mean. Callers (domain modules + plugins) decide what to audit and define their own typed payloads.

## Public interface (`__all__`)

```python
"AuditEntry",           # Pydantic model representing a row
"audit_for_ticket",     # write helpers (one per common entity kind)
"audit_for_pr",
"audit_for_repo",
"audit_for_reviewer_agent",
"audit_for_lesson",
"audit_for_review_job",
"audit_for_webhook_event",
"audit_for_workspace",
"audit",                # low-level generic write (escape hatch)
"list_for_entity",      # timeline read
"get",                  # single-entry read
"AuditEntryNotFoundError",
```

## `AuditEntry` model

Pydantic representation of a row, returned by the read API.

```python
class AuditEntry(BaseModel):
    id: UUID
    org_id: UUID
    entity_kind: str         # "ticket" / "pull_request" / "repo" / "reviewer_agent" / "lesson" / "review_job" / "webhook_event" / "workspace"
    entity_id: UUID          # loose ref — entity may have been deleted
    kind: str                # event type, follows <entity>.<verb_past> — e.g. "review_job.prompt_sent", "review_job.posted", "lesson.created"
    payload: dict[str, Any]  # already-serialized, free-form
    actor: Actor             # reconstructed from actor_kind + actor_login + actor_agent_id columns
    created_at: datetime
```

`Actor` is imported from `core/primitives`. The DB columns (`actor_kind`, `actor_login`, `actor_agent_id`) are reconstructed into a single `Actor` value object at read time.

## Write API

### Per-entity helpers

The primary way to write. Each helper hard-codes its `entity_kind`:

```python
async def audit_for_ticket(
    ticket_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
) -> None: ...

async def audit_for_pr(pr_id: UUID, kind: str, payload: BaseModel, *, actor: Actor, org_id: UUID) -> None: ...
async def audit_for_repo(repo_id: UUID, kind: str, payload: BaseModel, *, actor: Actor, org_id: UUID) -> None: ...
async def audit_for_reviewer_agent(agent_id: UUID, kind: str, payload: BaseModel, *, actor: Actor, org_id: UUID) -> None: ...
async def audit_for_lesson(lesson_id: UUID, kind: str, payload: BaseModel, *, actor: Actor, org_id: UUID) -> None: ...
async def audit_for_review_job(review_job_id: UUID, kind: str, payload: BaseModel, *, actor: Actor, org_id: UUID) -> None: ...
async def audit_for_webhook_event(webhook_event_id: UUID, kind: str, payload: BaseModel, *, actor: Actor, org_id: UUID) -> None: ...
async def audit_for_workspace(workspace_id: UUID, kind: str, payload: BaseModel, *, actor: Actor, org_id: UUID) -> None: ...
```

**Payload must be a Pydantic `BaseModel`.** Callers define typed payload classes in their own modules:

```python
# in domain/reviewer/audit_payloads.py
class PromptSentPayload(BaseModel):
    agent_name: str
    model_id_full: str
    token_estimate: int
    prompt_text_hash: str  # don't store full prompt — hash is enough for traceability
```

`audit_log` calls `.model_dump()` internally. Plain dicts are NOT accepted (forces good payload-shape hygiene at call sites).

### Generic escape hatch

```python
async def audit(
    entity_kind: str,
    entity_id: UUID,
    kind: str,
    payload: BaseModel,
    actor: Actor,
    org_id: UUID,
) -> None: ...
```

Used when none of the per-entity helpers fit (rare). Callers should prefer per-entity helpers for readability + grep-ability.

### What happens on write

1. Generate UUID for `id`; capture `created_at = now()`.
2. Validate `entity_kind` non-empty, `kind` non-empty.
3. Decompose `actor` into the three DB columns (`actor_kind` always set; `actor_login` set for `github_user`; `actor_agent_id` set for `agent`; both null for `system`).
4. Insert row.

No transaction wrapping — callers are expected to write audit entries within their existing transaction context (the session fixture from `core/database`). audit_log uses `get_session()` and joins the caller's transaction.

## Read API

### Timeline by entity

```python
async def list_for_entity(
    entity_kind: str,
    entity_id: UUID,
    *,
    limit: int = 50,
    before_ts: datetime | None = None,
    kinds: list[str] | None = None,
    org_id: UUID,
) -> list[AuditEntry]:
    """Returns entries for the given entity, newest first.
    Cursor pagination via `before_ts` (returns entries with created_at < before_ts).
    Optional filter on `kinds`.
    """
```

Used by the UI's per-ticket audit-log tab. The endpoint calling this passes `entity_kind='ticket'` plus the ticket's UUID; for the audit-log tab inside ticket detail.

### Single entry

```python
async def get(entry_id: UUID, *, org_id: UUID) -> AuditEntry:
    """Returns one entry or raises AuditEntryNotFoundError."""
```

Used for deep-linking (e.g., a shared URL pointing at one audit entry).

### No global feed in M01

`list_recent()` (a yaaof-wide timeline) is intentionally not exposed. The per-ticket audit log is the only consumer. Add when an ops view needs it.

## Indexes

Per data-model.md, two indexes on `audit_entries`:

- `(entity_kind, entity_id, created_at DESC)` — drives `list_for_entity`.
- `(org_id, created_at DESC)` — drives a future `list_recent` and global queries.

## Retention pruning

**Deferred for POC.** The 90-day retention target is documented in requirements but the prune implementation is not built in M01. The table grows unbounded until storage / query performance becomes a real concern; then a periodic prune loop is added.

When the time comes, the prune loop will:
- Be a plain `async def` loop started in FastAPI's `lifespan` (same shape as the workspace reaper); runs roughly once an hour.
- Delete rows where `created_at < now() - interval '90 days'`, scoped to current org.
- Emit a structured log line with rows-deleted count.

## What `core/audit_log` does NOT do

- Does not enforce a fixed `kind` taxonomy. Callers use any string they want. (A grep-able convention emerges naturally.)
- Does not validate `payload` shape beyond "must be a Pydantic model." The payload's schema is the caller's concern.
- Does not publish events. The `events` module is separate; if a caller wants both audit + event, they call both.
- Does not enforce FK integrity on `entity_id`. Loose ref. Entities can be deleted; audit survives.
- Does not provide a search API. Add when a real consumer needs it.

## Decisions

### 2026-05-14 — Per-entity write helpers + a generic escape hatch
Callers use `audit_for_ticket`, `audit_for_pr`, etc., which hard-code `entity_kind`. Read site stays simple (grep for `audit_for_ticket` to see all ticket writes). Generic `audit()` is the escape hatch for rare cases.

### 2026-05-14 — Payload must be a Pydantic model
`audit_log` accepts `BaseModel` and calls `.model_dump()` internally. Plain dicts are rejected. Forces callers to define typed payload classes.
**Why:** payload schema sprawl is real. Forcing types per kind discourages "just stuff random stuff in the dict."

### 2026-05-14 — Read API: `list_for_entity` (with cursor + kinds filter) + `get`; no global feed
M01 only needs per-ticket timelines.

### 2026-05-14 — Retention pruning deferred for POC
The 90-day target is documented; the implementation waits until table size or query perf forces it.
**Why:** POC scale doesn't justify the prune-job complexity. Postgres handles tens of thousands of rows on the relevant indexes without issue.
