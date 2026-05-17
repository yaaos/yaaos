# core/audit_log

> Append-only timeline primitive. Every meaningful action lands here.

## Purpose

Owns the `audit_entries` table and the API for writing and reading it. Domain modules and plugins write via per-entity helpers; the UI reads timelines via `list_for_entity`. No business logic — records facts, doesn't decide meaning. Callers define their own typed Pydantic payloads; the module serializes to JSONB.

## Public interface

Exports `AuditEntry`, `AuditEntryRow`, `AuditEntryNotFoundError`, generic `audit`, per-entity helpers (`audit_for_ticket`, `audit_for_pr`, `audit_for_lesson`, `audit_for_review_job`, `audit_for_webhook_event`, `audit_for_workspace`), and reads (`list_for_entity`, `get`). See `apps/backend/app/core/audit_log/__init__.py`.

No HTTP routes — the audit-log tab reads via a domain endpoint that delegates to `list_for_entity`.

## Module architecture

### `AuditEntry` shape

Carries `id`, `org_id`, `entity_kind`, `entity_id`, `kind`, `payload` (already-serialized via `model_dump(mode="json")`), `actor` (reconstructed from three DB columns), `created_at`. `Actor` is imported from `core/primitives`. The columns `actor_kind`, `actor_login`, `actor_agent_id` are recombined into one `Actor` at read time via `AuditEntry.from_row`.

### Write API

Per-entity helpers each hard-code their `entity_kind`. Signatures: `(entity_id, kind, payload, *, actor, org_id, session=None) -> AuditEntry`.

`payload` must be a Pydantic `BaseModel` — plain dicts raise `TypeError`. The module calls `.model_dump(mode="json")` internally so UUIDs/datetimes/enums are JSON-compatible. Forcing types discourages "stuff random stuff in the dict".

Each write:
1. Generates UUID for `id`.
2. Validates `entity_kind` and `kind` non-empty.
3. Decomposes `actor` into three columns (`actor_kind` always; `actor_login` for `github_user`; `actor_agent_id` for `agent`; both `None` for `system`).
4. Inserts the row.

Optional `session` joins the caller's transaction (helper adds + flushes; caller commits). Without it, the helper opens its own session, commits, refreshes. Callers writing audit alongside domain writes pass `session` to keep both in one transaction.

### Generic escape hatch

`audit(entity_kind, entity_id, kind, payload, actor, *, org_id, session=None)` when no per-entity helper fits. Prefer typed helpers for grep-ability.

### Read API

`list_for_entity(entity_kind, entity_id, *, org_id, limit=50, before_ts=None, kinds=None)` returns entries newest first, scoped to org. `before_ts` is the cursor (rows with `created_at < before_ts`); `kinds` filters by kind. Drives the per-ticket audit-log tab.

`get(entry_id, *, org_id)` returns one entry or raises `AuditEntryNotFoundError`. Used for deep-linking.

No global `list_recent()` feed — per-ticket timeline is the only consumer.

### What it does not do

- Does not enforce a fixed `kind` taxonomy — callers pick any string (a grep-able convention emerges).
- Does not validate payload shape beyond "must be Pydantic".
- Does not publish events — `core/events` is separate; callers wanting both call both.
- Does not enforce FK on `entity_id` — loose ref; entities can be deleted and the row survives.
- Does not prune — 90-day retention is the target; pruning lands when table size demands it.

## Data owned

- `audit_entries` — `(id, org_id, entity_kind, entity_id, kind, payload jsonb, actor_kind, actor_login, actor_agent_id, created_at)`. Indexes: `(entity_kind, entity_id, created_at)` for `list_for_entity`; `(org_id, created_at)` for global queries; `org_id` indexed independently.

## How it's tested

`app/core/audit_log/test/` is a placeholder. Exercised by every domain module that writes audit entries — integration tests touch real rows and assert on shape and ordering. Read API coverage rides on the per-ticket timeline endpoint's integration tests.
