# `domain/intake` — Internal Architecture

> Inbound VCS-event router. Receives `VCSEvent`s from plugins, applies filtering rules, parses re-review commands, syncs PR metadata, and dispatches into `tickets`, `pull_requests`, and `reviewer`.

## Purpose

`domain/intake` is the policy layer above the VCS plugins. Plugins emit raw semantic events; intake decides what to do with them. It owns no DB tables of its own. It coordinates writes across other modules.

Three external entry points:

1. **From plugins** (after webhook receipt): `await handle_vcs_events(events)`.
2. **From plugins' catch-up poller**: `await refresh_pr_metadata(repo_id, pr)` (when the plugin already has PR metadata from a list call) and `await refresh_pr_metadata_by_id(repo_id, pr_external_id)` (when only the id is known).
3. **From `reviewer`'s startup recovery**: same `refresh_pr_metadata*` helpers, used during crash recovery.

## Public interface (`__all__`)

```python
"handle_vcs_events",          # main entry point from plugins
"refresh_pr_metadata",        # called by plugins when they already have PR data
"refresh_pr_metadata_by_id",  # called when only the external id is known
"IntakeError",                # base exception (uncommon; most errors are audit-and-continue)
```

## Top-level dispatch

```python
async def handle_vcs_events(events: list[VCSEvent], *, org_id: UUID) -> None:
    """Process each event. Errors are isolated per event.
    org_id is resolved by the calling plugin (e.g., plugins/github maps webhook
    installation_id → org_id before invoking intake)."""
    for event in events:
        try:
            await _dispatch_one(event, org_id=org_id)
        except Exception:
            log.exception("intake.event_failed", event_kind=event.kind, source_event_id=event.source_event_id)
            await _audit_event_failed(event, org_id=org_id)
            # continue with next event
```

`_dispatch_one` is a `match` statement that calls the per-kind handler:

```python
async def _dispatch_one(event: VCSEvent, *, org_id: UUID) -> None:
    match event:
        case PullRequestReadyForReview():
            await handle_pr_ready_for_review(event, org_id=org_id)
        case PullRequestSynchronized():
            await handle_pr_synchronized(event, org_id=org_id)
        case PullRequestClosed():
            await handle_pr_closed(event, org_id=org_id)
        case PullRequestReopened():
            await handle_pr_reopened(event, org_id=org_id)
        case CommentCreated():
            await handle_comment_created(event, org_id=org_id)
        case ReactionAdded():
            await handle_reaction_added(event, org_id=org_id)
```

Per-kind handlers live in separate files (`intake/handlers/pr_ready_for_review.py`, etc.) for readability.

## Per-event handlers

### `handle_pr_ready_for_review(event)`

1. Resolve `repo` via `repos.get_by_external(event.plugin_id, event.repo_external_id)`. If not in allowlist → write `webhook_event.filtered(reason='not_allowlisted')`, return.
2. Apply filters on `event.pr`:
   - `pr.is_fork == true` → `webhook_event.filtered(reason='fork')`, return.
   - `pr.author_type == 'bot'` → `webhook_event.filtered(reason='bot_author')`, return.
   - (drafts already filtered upstream — the plugin doesn't emit `PullRequestReadyForReview` for drafts.)
3. Call `refresh_pr_metadata(repo.id, event.pr)` — upserts the `pull_requests` row + the linked ticket (creates ticket if missing).
4. Call `reviewer.schedule_review(ticket_id=ticket.id, agents='all')` — creates review_jobs for all three agents, subject to PerPRQueueDiscipline.

### `handle_pr_synchronized(event)`

1. Look up the PR via `pull_requests.get_by_external(event.plugin_id, event.pr_external_id)`. If unknown → log warning, return. (We never tracked this PR; ignore.)
2. Refresh PR metadata via `refresh_pr_metadata_by_id(pr.repo_id, event.pr_external_id)` (fetches fresh state from vcs).
3. **Trivial-diff check**: fetch the diff via `vcs.fetch_diff(event.pr_external_id)`. If all `diff.files` are on the skip list (from requirements) → `webhook_event.filtered(reason='trivial_diff')`, return.
4. **Force-push handling**: if `event.force_push == true`, call `vcs.mark_comments_outdated(event.pr_external_id, [...all prior comment ids...])`. (Fetched via `vcs.list_yaaof_comments`. For GitHub, this is a no-op; for future plugins, it may post follow-up threads.)
5. Call `reviewer.schedule_review(ticket_id, agents='all')` — PerPRQueueDiscipline cancels any in-flight jobs and reschedules with debounce.

### `handle_pr_closed(event)`

1. Look up the PR; if unknown, return.
2. Update PR state in `pull_requests` (state='closed' or 'merged' based on event).
3. Update the linked ticket to `status='complete'` (via `tickets.complete(ticket_id)`).
4. Call `reviewer.cancel_pending(ticket_id)` — cancels any queued review_jobs (per the per-PR queue discipline). Running jobs poll `review_jobs.status` at safe points and bail when they see `cancelled` (see [reviewer.md § Cancellation handling](reviewer.md#cancellation-handling-inside-the-handler)).

### `handle_pr_reopened(event)`

1. Update PR state to 'open'.
2. **No review trigger** (per requirements: reopen alone doesn't trigger; next commit will).

### `handle_comment_created(event)`

1. Look up the PR; if unknown, return.
2. Call `refresh_pr_metadata_by_id(...)` (sync PR metadata on every event per the rule).
3. **Skip if author is yaaof itself** (`event.author_login == yaaof_bot_login`). We don't re-trigger on our own comments.
4. Parse the body with the re-review regex:
   ```python
   _REREVIEW_RE = re.compile(r'@yaaof(?:-(?P<agent>architecture|security|style))?\s+rereview', re.IGNORECASE)
   m = _REREVIEW_RE.search(event.body)
   ```
5. If matched:
   - Write `ticket.rereview_requested(agent=m['agent'] or 'all', actor=Actor(github_user, login=event.author_login))`.
   - Call `reviewer.schedule_review(ticket_id, agents=m['agent'] or 'all')`.
   - Return.
6. Otherwise, check if this is an inline reply to a yaaof comment:
   - If `event.in_reply_to_comment_external_id` is set: query `posted_comments` table for that comment id → resolves `agent_id`.
   - If found: write `ticket.reply_received(agent_id, actor=Actor(github_user, login=...))`. Call `reviewer.schedule_reply(ticket_id, agent_id, reply_comment_id=event.comment_external_id)`.
   - If not found (parent isn't ours): ignore.

### `handle_reaction_added(event)`

1. Look up the comment via `posted_comments` table; resolves the agent. If not ours, ignore.
2. Write `ticket.reaction_received(agent_id, reaction=event.reaction, actor=...)`.
3. **No further action** in M01. Reactions are signal-only.

## Filtering rules

All filter decisions are centralized in intake (not in plugins) per the "plugins emit semantic events; intake filters" decision.

Filter sources:

- **Repo allowlist** — `repos.is_allowed(plugin_id, external_id)`.
- **PR draft state** — already filtered at plugin emission (no event emitted for draft).
- **Fork** — `pr.is_fork`.
- **Bot author** — `pr.author_type == 'bot'`.
- **Trivial diff** — per requirements, diff containing only skip-list files (lockfiles, vendored, binary, linguist-generated, conventions like `*.pb.go`).
- **Diff too large** — `len(diff.lines_changed) > 5000`. Skipped with reason='too_large'. (Implemented as part of the trivial-diff check in `handle_pr_synchronized`.)

Each filter that drops an event writes `webhook_event.filtered` with `payload={'reason': <reason>, 'event_kind': <kind>}`.

## PR metadata sync

Two functions, both upsert the `pull_requests` row and sync the linked ticket's title + description:

```python
async def refresh_pr_metadata(repo_id: UUID, pr: VCSPullRequest, *, org_id: UUID) -> PullRequest:
    """Use when the caller already has fresh PR data (e.g., from a webhook payload).
    Upserts pull_requests row; ensures a ticket exists for this PR; syncs title/description."""

async def refresh_pr_metadata_by_id(repo_id: UUID, pr_external_id: str, *, org_id: UUID) -> PullRequest:
    """Use when the caller only has the external id (e.g., catch-up poller, or an event we got
    that didn't include full PR data). Fetches via vcs.fetch_pr and delegates to refresh_pr_metadata."""
```

Why two: webhook payloads include the full PR object, so no API call needed. Catch-up only has the id and must fetch. Sharing one signature would force unnecessary fetches.

Upsert semantics:

- If `pull_requests` row exists: update mutable fields (shas, draft, state, title, body, last_synced_at).
- If not exists: insert + create a ticket with `source='github_pr'`, `status='in_review'`.
- Ticket title + description always re-synced from the PR. (Per requirements: "every webhook syncs metadata.")

## Re-review command parser

Single regex, compiled once at module load:

```python
_REREVIEW_RE = re.compile(
    r'@yaaof(?:-(?P<agent>architecture|security|style))?\s+rereview',
    re.IGNORECASE,
)
```

Matches:
- `@yaaof rereview` → agent=None (means "all three")
- `@yaaof-architecture rereview` → agent='architecture'
- Case-insensitive on the trigger; whitespace tolerant.

Body-parsed token, NOT a GitHub user mention (no GitHub user `yaaof` exists). See requirements.md note on this.

## Reply-agent lookup

Uses the `posted_comments` table (owned by `reviewer`, see data-model.md):

```sql
CREATE TABLE posted_comments (
    external_comment_id text PRIMARY KEY,
    org_id uuid NOT NULL,
    pr_id uuid NOT NULL REFERENCES pull_requests(id),
    review_job_id uuid NOT NULL REFERENCES review_jobs(id),
    agent_id uuid NOT NULL REFERENCES reviewer_agents(id),
    posted_at timestamptz NOT NULL
);
```

`reviewer` inserts one row per posted comment when finishing a review job. `intake` reads by `external_comment_id` to find which agent owns a comment.

## Audit log entries intake writes

Audit kinds follow the `<entity>.<verb_past>` convention from [patterns.md § Audit log discipline](../patterns.md#audit-log-discipline); entity prefix matches the helper being called. Each kind has a named Pydantic payload class defined in `intake/audit_payloads.py`.

| Kind | Helper | Payload class |
|---|---|---|
| `webhook_event.filtered` | `audit_for_webhook_event` | `WebhookFilteredPayload { reason, event_kind, source_event_id }` |
| `ticket.rereview_requested` | `audit_for_ticket` | `RereviewRequestedPayload { agent_name_or_all, comment_external_id }` |
| `ticket.reply_received` | `audit_for_ticket` | `ReplyReceivedPayload { agent_id, parent_comment_external_id, new_comment_external_id }` |
| `ticket.reaction_received` | `audit_for_ticket` | `ReactionReceivedPayload { agent_id, reaction, target_comment_external_id }` |
| `webhook_event.failed` | `audit_for_webhook_event` | `WebhookFailedPayload { event_kind, source_event_id, exception_type, message }` |

## Error handling

Per-event isolation. If a single event raises, we:

1. Log the exception with structured context (event kind, source_event_id).
2. Write `webhook_event.failed` audit entry.
3. Continue with the next event in the batch.

The batch (typically one event per webhook) responds 200 to the plugin regardless. The plugin doesn't retry; manual investigation via audit + logs.

For systematic failures (e.g., database down): exceptions propagate up to the webhook receiver in plugins/github, which logs + responds 200 anyway. Webhooks are best-effort delivery; missed events are handled by the catch-up poller on next restart.

## What `domain/intake` does NOT do

- Does not own any DB tables.
- Does not run the catch-up poller (that's the plugin's bootstrap task; it calls into intake).
- Does not decide review verdicts, parse findings, or post reviews — that's `reviewer`.
- Does not own the PR row schema — that's `pull_requests` (intake calls its upsert helper).
- Does not validate ticket state machine transitions — that's `tickets`.
- Does not retry events. One pass per webhook; failures captured in audit.

## Decisions

### 2026-05-14 — One handler function per event kind; top-level wrapper dispatches
Each event kind gets its own file (e.g., `handlers/pr_ready_for_review.py`). Readability over compactness.

### 2026-05-14 — Single regex for `@yaaof rereview` command parsing
`r'@yaaof(?:-(?P<agent>architecture|security|style))?\s+rereview'`, case-insensitive. Simple, single source of truth.

### 2026-05-14 — Reply-agent lookup uses the new `posted_comments` table
`reviewer` writes on every post; `intake` reads by `external_comment_id`. Indexed B-tree lookup. New table added to data-model.

### 2026-05-14 — Two PR-metadata-sync functions (from-payload + from-fetch)
Webhook callers already have the PR object; catch-up needs to fetch. Two signatures, shared internal upsert.

### 2026-05-14 — Per-event try/except; bad events don't poison the batch
Failures logged + audited + skipped. Webhook receiver responds 200 regardless. No automatic retries.

### 2026-05-14 — Filtering centralized in intake, not in plugins
Plugins emit semantic events (e.g., `PullRequestReadyForReview` only when ready, not when drafts). Intake applies remaining filters (fork, bot author, repo allowlist, trivial/too-large diff). One place for filter rules.
