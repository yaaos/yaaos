# core/audit_log

> Append-only timeline primitive. Every meaningful action lands here.

## Scope

- Owns: `audit_entries` table; write + read API.
- Does NOT own: SSE events (that's [`core/sse`](core_sse.md)), FK integrity on `entity_id` (loose ref — entities can be deleted and rows survive), or a fixed `kind` taxonomy (callers pick strings; convention enforced by grep).
- No HTTP routes — the audit tab reads via a domain endpoint delegating to `list_for_entity`.

## Why / invariants

**Payload must be Pydantic** — `audit(..., payload: BaseModel)` calls `.model_dump(mode="json")` internally. Plain dicts raise `TypeError`. Enforced to prevent untyped dict dumps that drift.

**`session` parameter is optional** — pass it to join the caller's transaction (helper adds + flushes; caller commits). Without it, the helper opens its own session and commits. Callers writing audit alongside domain writes must pass `session`.

**Retention** — `purge_older_than(cutoff)` called daily by `core/identity.scheduler` with a 15-day cutoff (`AUDIT_LOG_RETENTION`). Lowered from 30d to absorb MCP dispatch volume (one row per JSON-RPC method).

**Actor decomposition** — five DB columns (`actor_kind`, `actor_login`, `actor_agent_id`, `actor_user_id`, `actor_workspace_id`) reassembled into one `Actor` at read time via `AuditEntry.from_row`. The `Actor` type lives in `core/audit_log/actor.py` and is re-exported from the package.

## Gotchas

- `list_for_org` caps at 500 per call. `list_for_entity` caps at 50 (cursor via `before_ts`).
- Per-entity helpers (`audit_for_ticket`, `audit_for_pr`, etc.) hard-code `entity_kind` — prefer them over generic `audit()` for grep-ability.
- Login/logout emits one row **per org membership**. Users with zero memberships emit nothing.

