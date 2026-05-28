# domain/intake

> Single inbound-signal endpoint — plugins register `IntakeType` handlers; `POST /api/intake/{type}` verifies, dedups, and either creates a ticket + starts a workflow or applies a side-effect.

## Scope

Owns: webhook routing policy, idempotency layers, skip-path heuristics, rereview/command parsing. Coordinates writes to `tickets`, `pull_requests`, `reviewer`, `core/audit_log` via plugin-supplied handlers.

Does NOT own: any tables. All writes flow through other modules' services.

## Why / invariants

- **HMAC verification** happens inside each `IntakeType.handle()` before any state mutation. `IntakeRejectedError(kind="bad_signature")` → 401. Never trust the body before the signature clears.
- **Two idempotency layers:** delivery-level (github type dedupes on `source_event_id`) and ticket-level (`IntakePrepared.idempotency_key` unique on `tickets`; plus `(org_id, source, source_external_id)` for concurrent deliveries).
- **Filtering audit trail:** every dropped event writes `webhook_event.filtered` with `{reason, event_kind, source_event_id}` — the log shows why nothing happened.
- `is_skippable_path` is the single source of truth for the trivial-diff skip list; `domain/reviewer` re-imports it.

## Registered handlers

`github` (in `plugins/github.intake_type`) — one entry for every GitHub event; branches on `X-Github-Event` + `payload.action`. See [plugins_github.md](plugins_github.md).

## Gotchas

- `@yaaos rereview` is a body-parsed token, not a GitHub mention. Legacy `@yaaos-<specialty>` still matches (specialty ignored).
- The callback path for OAuth under `/api/mcp-proxy` is the only `public_route` exception; this endpoint is also exempt from session-cookie auth (`public_route` pattern).
- Missed events recover via the plugin's catch-up poller, not replay here.

## Data owned

None.

## How it's tested

- `test/test_parsing.py` — `parse_rereview`, `parse_yaaos_command`, `is_skippable_path` exhaustively.
- `test/test_intake_endpoint.py` — happy path (ticket + workflow + outbox row), unknown type (404), bad signature (401), duplicate idempotency key.
- Per-plugin handler logic in `app/plugins/github/test/`.
