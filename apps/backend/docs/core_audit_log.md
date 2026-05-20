# core/audit_log

> Append-only timeline primitive. Every meaningful action lands here.

## Purpose

Owns the `audit_entries` table and the API for writing and reading it. Domain modules and plugins write via per-entity helpers; the UI reads timelines via `list_for_entity`. No business logic — records facts, doesn't decide meaning. Callers define their own typed Pydantic payloads; the module serializes to JSONB.

## Public interface

Exports `AuditEntry`, `AuditEntryRow`, `AuditEntryNotFoundError`, generic `audit`, per-entity helpers (`audit_for_ticket`, `audit_for_pr`, `audit_for_lesson`, `audit_for_review_job`, `audit_for_webhook_event`, `audit_for_workspace`), and reads (`list_for_entity`, `get`). See `apps/backend/app/core/audit_log/__init__.py`.

No HTTP routes — the audit-log tab reads via a domain endpoint that delegates to `list_for_entity`.

## Module architecture

### `AuditEntry` shape

Carries `id`, `org_id`, `entity_kind`, `entity_id`, `kind`, `payload` (already-serialized via `model_dump(mode="json")`), `actor` (reconstructed from five DB columns), `created_at`. `Actor` is owned by this module (`core/audit_log/actor.py`) and re-exported from the package — the row's `actor` column is what defines its shape, so the type lives where it's used. The columns `actor_kind`, `actor_login`, `actor_agent_id`, `actor_user_id`, `actor_workspace_id` are recombined into one `Actor` at read time via `AuditEntry.from_row`. M02 added the two `actor_*_id` columns to round-trip the additive `user` / `workspace` actor kinds; `sso` populates only `actor_login` (the IdP-asserted email).

### Write API

Per-entity helpers each hard-code their `entity_kind`. Signatures: `(entity_id, kind, payload, *, actor, org_id, session=None) -> AuditEntry`.

`payload` must be a Pydantic `BaseModel` — plain dicts raise `TypeError`. The module calls `.model_dump(mode="json")` internally so UUIDs/datetimes/enums are JSON-compatible. Forcing types discourages "stuff random stuff in the dict".

Each write:
1. Generates UUID for `id`.
2. Validates `entity_kind` and `kind` non-empty.
3. Decomposes `actor` into five columns (`actor_kind` always; `actor_login` for `github_user` / `sso`; `actor_agent_id` for `agent`; `actor_user_id` for `user`; `actor_workspace_id` for `workspace`; all id fields `None` for `system`).
4. Inserts the row.

Optional `session` joins the caller's transaction (helper adds + flushes; caller commits). Without it, the helper opens its own session, commits, refreshes. Callers writing audit alongside domain writes pass `session` to keep both in one transaction.

### Generic escape hatch

`audit(entity_kind, entity_id, kind, payload, actor, *, org_id, session=None)` when no per-entity helper fits. Prefer typed helpers for grep-ability.

### Read API

`list_for_entity(entity_kind, entity_id, *, org_id, limit=50, before_ts=None, kinds=None)` returns entries newest first, scoped to org. `before_ts` is the cursor (rows with `created_at < before_ts`); `kinds` filters by kind. Drives the per-ticket audit-log tab.

`list_for_org(*, org_id, actor_kinds=None, actions=None, before_ts=None, after_ts=None, limit=50)` returns the cross-entity org feed used by the `/api/audit` endpoint and the Owner/Admin Audit page. Newest first; capped at 500 per call.

`get(entry_id, *, org_id)` returns one entry or raises `AuditEntryNotFoundError`. Used for deep-linking.

`purge_older_than(cutoff)` deletes rows with `created_at < cutoff`. The daily retention task in `domain/identity.scheduler` calls this with `datetime.now(UTC) - AUDIT_LOG_RETENTION` (`AUDIT_LOG_RETENTION = timedelta(days=15)`, exported from `core/audit_log`). Lowered from 30d in M04 — MCP dispatch writes one audit row per JSON-RPC method and is the dominant volume contributor; 15d keeps the storage envelope bounded for the POC.

### What it does not do

- Does not enforce a fixed `kind` taxonomy — callers pick any string (a grep-able convention emerges).
- Does not validate payload shape beyond "must be Pydantic".
- Does not publish events — `core/events` is separate; callers wanting both call both.
- Does not enforce FK on `entity_id` — loose ref; entities can be deleted and the row survives.
- Does prune as of M02 — `purge_older_than(cutoff)` plus the daily scheduler call in `domain/identity.scheduler` keeps `audit_entries` within `AUDIT_LOG_RETENTION` (15 days, lowered from 30d in M04 Phase 6 to absorb MCP dispatch volume).

## Data owned

- `audit_entries` — `(id, org_id, entity_kind, entity_id, kind, payload jsonb, actor_kind, actor_login, actor_agent_id, actor_user_id, actor_workspace_id, created_at)`. Indexes: `(entity_kind, entity_id, created_at)` for `list_for_entity`; `(org_id, created_at)` for global queries; `org_id` indexed independently.

### M02 action catalogue

The `kind` column is free-form, but the M02 emitters use a small grep-friendly vocabulary:

| Entity | Kinds |
|---|---|
| `invitation` | `invited` |
| `membership` | `joined`, `removed`, `role_changed` |
| `user` | `logged_in`, `logout`, `logout_all` |

Login/logout emits one row per org the user is a member of, since the row schema requires `org_id`. Users with zero memberships emit nothing.

## How it's tested

`app/core/audit_log/test/` is a placeholder. Exercised by every domain module that writes audit entries — integration tests touch real rows and assert on shape and ordering. Read API coverage rides on the per-ticket timeline endpoint's integration tests.
