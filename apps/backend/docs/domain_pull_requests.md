# domain/pull_requests

> VCS-mirror module — owns `pull_requests`, the PR aggregate, and `PRState`. Pure mirror state, no review logic.

## Purpose

yaaos's local copy of VCS-side PR state. Persists PR metadata (shas, branches, draft/fork flags, title/body, html_url, sync timestamps); exposes upsert + state-transition + read APIs; silent — does not publish events. User-visible state changes flow through `tickets.TicketStatusChanged`. ReviewJob aggregate and per-PR queue discipline live in `reviewer`.

## Public interface

Exported from `app/domain/pull_requests/__init__.py`:

- Aggregate + state — `PullRequest`, `PRState` (`Literal["open", "closed", "merged"]`).
- Operations — `upsert` (create/refresh from a `VCSPullRequest`), `update_state` (explicit transition), `get` (by UUID), `get_by_external` (by `(plugin_id, external_id)`), `list_by_ids` (batch read by id list).
- Exceptions — `PullRequestNotFoundError`.

No HTTP routes — the UI lists tickets, not PRs.

## Module architecture

### `PullRequest` aggregate

Pydantic model mirroring the row. `PullRequest.from_row(row)` converts an ORM row. `PRState` mirrors VCS semantics. Schema in `app/domain/pull_requests/models.py`.

### `upsert`

Called by `intake` on every webhook for a tracked PR. Insert if `(plugin_id, external_id)` is new; otherwise refresh mutable fields and stamp `last_synced_at`.

- Service signature: required `session: AsyncSession`; never commits. The caller composes ticket-insert + PR-upsert + audit + workflow-start in one transaction so the FK on `pull_requests.ticket_id` resolves before commit.
- On insert, `ticket_id` is required (raises `ValueError` otherwise).
- On update, `ticket_id` is ignored — the existing FK stays.
- Mutable: `title`, `body`, `base_sha`, `head_sha`, `is_draft`, `state`, `html_url`, `last_synced_at`.
- Immutable: `plugin_id`, `external_id`, `number`, `repo_external_id`, `ticket_id`, `author_*`, `base_branch`, `head_branch`, `is_fork`. Branch renames not modelled.
- Writes `audit_for_pr(kind="pull_request.synced", payload={changed_fields})` only when something changed.

### `update_state`

Updates the `state` column. No state-machine validation — VCS is source of truth, yaaos copies. Raises `PullRequestNotFoundError` on missing id; no-ops if state matches. Writes `audit_for_pr(kind="pull_request.state_changed", payload={from_state, to_state})`. Called by the github intake type's `pull_request.closed` and `pull_request.reopened` branches.

### Reads

- `get(pr_id, *, org_id)` — raises `PullRequestNotFoundError` if absent.
- `get_by_external(plugin_id, external_id, *, org_id)` — returns `PullRequest | None`. Used by `intake` to check whether an event matches a tracked PR.
- `list_by_ids(pr_ids)` — batch read; returns only the ids that exist, silently omitting missing ones. No org_id scoping — callers hold org context from the tickets they already fetched. Empty input short-circuits without a DB hit. Used by `tickets.list_tickets` to batch-enrich PR metadata.

No filter/sort API — the UI surfaces tickets, and direct PR queries are by id or external id.

### Silent module

Publishes no events. User-visible state changes flow through `tickets.TicketStatusChanged`. Audit-log writes (`actor=Actor.system()`) carry the trace.

### What it doesn't own

- `review_jobs` and the per-PR queue → `reviewer`.
- Ticket state → `tickets`.
- The decision to review a PR → `intake`.
- Outdated-comment markers → handled at `vcs.mark_comments_outdated` (no-op on GitHub, which marks automatically).

## Data owned

- `pull_requests` — `(id, org_id, plugin_id, external_id, repo_external_id, ticket_id → tickets.id, number, title, body, author_login, author_type, base_branch, head_branch, base_sha, head_sha, is_draft, is_fork, state, html_url, last_synced_at, created_at, updated_at)`. Unique on `(plugin_id, external_id)`; `org_id` indexed.

## How it's tested

- `test_upsert_session.py` — session-ownership contract for `upsert` (insert + update paths, FK safety, missing ticket_id guard).
- `test_service.py` — service tests (`@pytest.mark.service`) for `list_by_ids`: full match, empty input, unknown ids, partial match.
- Upsert/state/read paths are also covered indirectly by `intake`'s integration tests driving real webhook payloads through `upsert` → `update_state`.
