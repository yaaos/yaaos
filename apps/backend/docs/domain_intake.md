# domain/intake

> Inbound VCS-event router — receives `VCSEvent`s from plugins, applies filters, parses re-review commands, syncs PR metadata, and dispatches into `tickets`, `pull_requests`, and `reviewer`.

## Purpose

The policy layer above VCS plugins. Plugins emit raw semantic events; intake decides what to do. Owns no DB tables — coordinates writes across other modules. Three external entry points: plugin webhook handlers (`handle_vcs_events`), the catch-up poller (`refresh_pr_metadata*`), and the reviewer's startup recovery (also via `refresh_pr_metadata*`). Every filter decision and every metadata sync flows through here.

## Public interface

Exported from `app/domain/intake/__init__.py`:

- `handle_vcs_events(events, *, org_id)` — main entry from plugins.
- `refresh_pr_metadata(repo_external_id, pr, *, org_id)` — caller has a `VCSPullRequest`.
- `refresh_pr_metadata_by_id(repo_external_id, pr_external_id, *, org_id)` — only external id known (catch-up).
- `parse_rereview(body)` — pure helper, returns `(matched, agent_name | None)`.
- `is_skippable_path(path)` — pure helper; `True` for lockfiles, vendor dirs, generated files, binary extensions. Also used by `domain/reviewer` for its trivial-diff check.
- `IntakeError` — base exception; uncommon (most errors are audit-and-continue).

No HTTP routes. Webhook surface lives in the VCS plugin (e.g., `/api/github/webhook`); the plugin verifies HMAC, parses into `VCSEvent`s, and calls `handle_vcs_events`.

## Module architecture

### Files

- `service.py` — entry points, per-event handlers, audit-payload models, `refresh_pr_metadata*` upsert.
- `parsing.py` — `@yaaos rereview` regex and skip-path heuristics. Pure, unit-tested.
- `module.py` — `get_module_name() -> "intake"`.

### Top-level dispatch

`handle_vcs_events` wraps each event in try/except. A single bad event writes `webhook_event.failed` with `{event_kind, source_event_id, exception_type, message}`; the loop continues. The plugin receives 200 regardless. Missed events are recovered by the plugin's catch-up poller on restart. `_dispatch_one` is an `isinstance` chain over the six concrete subclasses.

### Per-event handlers

- `_handle_pr_ready_for_review` — filters forks and bot authors (writing `webhook_event.filtered`), calls `refresh_pr_metadata`, then `reviewer.schedule_review(trigger_reason="pr_ready")`. No repo-allowlist gate — the GitHub App install picks the access scope.
- `_handle_pr_synchronized` — looks up the PR, refreshes via the by-id variant (fresh VCS fetch), schedules a review with `trigger_reason="pr_synchronized"`. The reviewer's debounce + per-PR queue collapses bursts.
- `_handle_pr_closed` — updates PR state to `merged` or `closed`, transitions the ticket to `complete` if `in_review`, calls `reviewer.cancel_pending`.
- `_handle_pr_reopened` — updates PR state to `open`. No review triggered — the next commit does so via `pr_synchronized`.
- `_handle_comment_created` — skips yaaos bot comments (`YAAOS_BOT_LOGIN = "yaaos[bot]"`) and `author_type == "bot"`. Then:
  1. **Re-review command** — `parse_rereview(event.body)`. On match: write `ticket.rereview_requested` with the GitHub user as actor, call `reviewer.schedule_review` with `trigger_reason="rereview_command"`.
  2. **Inline replies are deferred.** A future `review_comments` table will own that lifecycle; intake silently drops them today.
- `_handle_reaction_added` — looks up the comment in `posted_comments`. On hit: write `ticket.reaction_received`. Reactions are signal-only — no review triggered.

### Filtering rules

Centralized here, not in plugins. Plugins emit semantic events (e.g., `PullRequestReadyForReview` only when actually ready, not drafts); intake applies fork, bot author, trivial diff (delegated to `reviewer` via `is_skippable_path`), too-large diff (delegated to `reviewer`, 5000-line threshold). Drops write `webhook_event.filtered` with `{reason, event_kind, source_event_id}`.

### `@yaaos rereview` parser

Single case-insensitive regex compiled once: `@yaaos(?:-[a-z0-9-]+)?\s+rereview`. Legacy `@yaaos-<specialty>` forms still match for backwards compatibility; the specialty is ignored (one reviewer per ticket). Body-parsed token, not a GitHub user mention. Whitespace tolerant. Definition in `app/domain/intake/parsing.py`.

### Skip-path heuristics

`is_skippable_path` matches the requirements' trivial-diff skip list: lockfiles (`package-lock.json`, `yarn.lock`, `Cargo.lock`, `poetry.lock`, `Pipfile.lock`, `Gemfile.lock`, `go.sum`), vendor dirs (`node_modules/`, `vendor/`, `third_party/`, `dist/`, `build/`, `out/`), generated conventions (`*.pb.go`, `*.gen.*`, `_generated` substring), and binary extensions (images, archives, fonts, PDFs). Re-imported by `domain/reviewer.queue._is_skip_path` — intake is the single source of truth.

### PR metadata sync

Two functions share an internal upsert. Use `refresh_pr_metadata` when the caller has a `VCSPullRequest` (webhooks include it); use `refresh_pr_metadata_by_id` when only the external id is known (catch-up poller, reviewer recovery — fetches via `vcs.get_plugin("github").fetch_pr`).

Handles the chicken-and-egg between `tickets.pr_id` and `pull_requests.ticket_id`:

1. If the `pull_requests` row exists, call `pull_requests.upsert(pr)`, then re-sync ticket `title`/`description` via `_sync_ticket_titles` (no audit — metadata sync is not a status transition).
2. If not, insert a `TicketRow` first (status=`in_review`, `pr_id=None`), then `pull_requests.upsert(pr, ticket_id=ticket_id)`, then backfill `tickets.pr_id`. Writes `ticket.created` and publishes `TicketStatusChanged(previous=None, new='in_review')`.

This is the production write path for tickets. `tickets.create_for_pr` exists for direct callers and tests.

### Audit-log entries written

| Kind | When | Payload |
|---|---|---|
| `webhook_event.filtered` | A filter rule rejects an event | `{reason, event_kind, source_event_id}` |
| `webhook_event.failed` | Per-event try/except catches an exception | `{event_kind, source_event_id, exception_type, message}` |
| `ticket.created` | First-time PR upsert creates the ticket | `{pr_id, repo_external_id}` |
| `ticket.rereview_requested` | `@yaaos rereview` comment matched | `{comment_external_id}` |
| `ticket.reaction_received` | Reaction added to a yaaos comment | `{reaction, target_comment_external_id}` |

`webhook_event.*` entries use a synthetic UUID as entity id (intake doesn't have the webhook row id at this layer).

### Error handling

Per-event isolation only. Systematic failures (DB down) propagate to the plugin's webhook receiver, which logs and still responds 200 — webhooks are best-effort and missed events recover via the catch-up poller. No retries inside intake.

## Data owned

None. Writes through `tickets`, `pull_requests`, `reviewer`, and `core/audit_log`.

## How it's tested

`app/domain/intake/test/test_parsing.py` covers `parse_rereview` and `is_skippable_path` exhaustively (every agent variant, case-insensitivity, negatives, every skippable category). Dispatch + handler logic covered by backend integration tests in `app/test/` — drive real `VCSEvent` instances through `handle_vcs_events` and assert ticket / PR / review_job rows and audit entries.
