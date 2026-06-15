# domain/intake

> Single inbound-signal endpoint — plugins register `IntakeType` handlers; `POST /api/intake/{type}` verifies, dedups, and applies a side-effect or returns a side-effect response.

## Scope

Owns: webhook routing policy, idempotency layers, skip-path heuristics, rereview/command parsing. All handlers return `IntakeSideEffect`; ticket creation and workflow dispatch happen inside each plugin's `handle()` via the domain services.

Does NOT own: any tables. All writes flow through other modules' services.

## Why / invariants

- **HMAC verification** happens inside each `IntakeType.handle()` before any state mutation. `IntakeRejectedError(kind="bad_signature")` → 401. Never trust the body before the signature clears.
- **All handlers return `IntakeSideEffect`.** There is no `IntakePrepared` branch — ticket creation + workflow dispatch happen inside each plugin's `handle()`. The endpoint handler is a thin wrapper that calls `handle()` and returns the side-effect.
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
- `test/test_intake_endpoint.py` — happy path returns side-effect, default detail when not specified, unknown type (404).
- Per-plugin handler logic in `app/plugins/github/test/`.
