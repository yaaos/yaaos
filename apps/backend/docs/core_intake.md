# core/intake

> Single inbound-signal endpoint — plugins register `IntakeType` handlers and `IntakePoint`s; `POST /api/intake/{type}` verifies, dedups, and applies a side-effect or returns a side-effect response.

## Scope

Owns: webhook routing policy, idempotency layers, skip-path heuristics, rereview/command parsing, and the `IntakePoint` registry (plugin-contributed trigger sources `domain/repos` trigger bindings target). All `IntakeType` handlers return `IntakeSideEffect`; ticket creation and run dispatch happen inside each plugin's `handle()` via the domain services.

Does NOT own: any tables. All writes flow through other modules' services.

## Why / invariants

- **HMAC verification** happens inside each `IntakeType.handle()` before any state mutation. `IntakeRejectedError(kind="bad_signature")` → 401. Never trust the body before the signature clears.
- **All handlers return `IntakeSideEffect`.** There is no `IntakePrepared` branch — ticket creation + run dispatch happen inside each plugin's `handle()`. The endpoint handler is a thin wrapper that calls `handle()` and returns the side-effect.
- **Filtering audit trail:** every dropped event writes `webhook_event.filtered` with `{reason, event_kind, source_event_id}` — the log shows why nothing happened.
- `is_skippable_path` is the single source of truth for the trivial-diff skip list.
- **Registry is the standard ContextVar pattern** (production rides the import-time default; `set_intake_for_tests` is the sole test seam) — holds both `IntakeType`s (keyed by `name`) and `IntakePoint`s (keyed by `id`). `IntakePoint(id, plugin_id, label, kind)` is a plugin-contributed trigger source; `domain/repos.add_binding` validates its `intake_point_id` against `list_intake_points()`, and `TriggerBinding.schedule` must be present iff the point's `kind == "schedule"`.

## Registered handlers

`github` (in `plugins/github.intake_type`) — one entry for every GitHub event; branches on `X-Github-Event` + `payload.action`. See [plugins_github.md](plugins_github.md).

## Registered intake points

`github:pr_opened`, `github:pr_commits`, `github:pr_comment` (all `kind="webhook"`, registered by `plugins/github.bootstrap()`) and `schedule` (`kind="schedule"`, `plugin_id=None`, registered by `domain/pipelines` at import time). `GET /api/intake/points` lists all four — the Repos-page trigger picker's data source.

## Gotchas

- `parse_yaaos_command` recognizes the canonical `@yaaos re-review` / `@yaaos cancel` grammar (body-parsed tokens, not GitHub mentions) — returns `"re-review" | "cancel" | None`. The deprecated `@yaaos rereview` form (no hyphen; legacy `@yaaos-<specialty>` still matches, specialty ignored) also maps to `"re-review"` via `parse_rereview`, kept for backward compat.
- The callback path for OAuth under `/api/mcp-proxy` is the only `public_route` exception; this endpoint is also exempt from session-cookie auth (`public_route` pattern).
- Missed events recover via the plugin's catch-up poller, not replay here.
- `POST /api/intake/{type}` stays unclassified in `core/auth`'s route-security taxonomy (relies on its own `Depends(public_route)`, not a prefix rule) so the GitHub webhook never gains an `X-Yaaos-Org-Slug`/CSRF requirement; `GET /api/intake/points` is ORG_SCOPED via a single method+path exact override (`ORG_SCOPED_METHOD_EXACT`), not a blanket `/api/intake` prefix.

## Data owned

None.

## How it's tested

- `test/test_parsing.py` — `parse_rereview`, `parse_yaaos_command`, `is_skippable_path` exhaustively.
- `test/test_intake_endpoint.py` — happy path returns side-effect, default detail when not specified, unknown type (404).
- Per-plugin handler logic in `app/plugins/github/test/`.
