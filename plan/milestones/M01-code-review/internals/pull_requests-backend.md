# `domain/pull_requests` — Internal Architecture

> VCS-mirror module. Owns the `pull_requests` table, the PR aggregate, and the `PRState` enum. Pure mirror state — no review logic, no queue logic, no events.

## Purpose

`domain/pull_requests` is yaaof's local copy of GitHub-side PR state. It:

- Persists PR metadata (shas, branches, draft/fork flags, title/body, html_url, sync timestamps).
- Exposes upsert + state-transition + read APIs.
- Is silent — does not publish events. User-visible state changes go through `tickets.TicketStatusChanged`.

This module is purely the VCS mirror; the ReviewJob aggregate and per-PR queue discipline live in `reviewer`.

## Public interface (`__all__`)

```python
"PullRequest",            # the aggregate root
"PRState",                # enum: "open" | "closed" | "merged"

"upsert",                 # create or update from a fresh VCSPullRequest
"update_state",           # explicit state transition on close/merge
"get",                    # by yaaof UUID
"get_by_external",        # by (plugin_id, external_id)

"PullRequestNotFoundError",
```

## `PullRequest` model

```python
class PullRequest(BaseModel):
    id: UUID
    org_id: UUID
    plugin_id: str
    external_id: str
    repo_id: UUID
    ticket_id: UUID                # always set in M01
    number: int
    title: str
    body: str | None
    author_login: str
    author_type: Literal["user", "bot"]
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    is_draft: bool
    is_fork: bool
    state: PRState
    html_url: str
    last_synced_at: datetime
    created_at: datetime
    updated_at: datetime
```

`PRState` is a string-valued enum: `"open"`, `"closed"`, `"merged"`. Mirrors GitHub semantics.

## Functions

### `upsert`

```python
async def upsert(
    pr: VCSPullRequest,
    repo_id: UUID,
    *,
    ticket_id: UUID | None = None,   # caller passes when creating a new row
    org_id: UUID,
) -> PullRequest:
    """Insert if (plugin_id, external_id) is new; otherwise update mutable fields.
    Sets last_synced_at = now() on every call.

    On insert, `ticket_id` is required. On update, it's ignored (existing FK
    stays put).

    Mutable fields refreshed on every call: title, body, base_sha, head_sha,
    is_draft, state, html_url, last_synced_at. Immutable after insert:
    plugin_id, external_id, number, repo_id, ticket_id, author_*, base_branch,
    head_branch (we don't model branch renames in M01), is_fork.
    """
```

Called by `intake.refresh_pr_metadata` on every webhook for a tracked PR.

Writes `audit_for_pr(pr_id, kind='pull_request.synced', payload={changed_fields}, actor=Actor(system))` — only on actual changes (skips if all fields unchanged).

### `update_state`

```python
async def update_state(
    pr_id: UUID,
    new_state: PRState,
    *,
    org_id: UUID,
) -> None:
    """Updates state column. No state-machine validation in M01 — caller
    (intake) is trusted to send valid transitions.

    Writes audit_for_pr(kind='pull_request.state_changed', payload={from, to}).
    """
```

Called by `intake.handle_pr_closed` (sets state to `closed` or `merged`) and `intake.handle_pr_reopened` (sets to `open`).

### `get` / `get_by_external`

```python
async def get(pr_id: UUID, *, org_id: UUID) -> PullRequest:
    """Raises PullRequestNotFoundError if not found."""

async def get_by_external(
    plugin_id: str,
    external_id: str,
    *,
    org_id: UUID,
) -> PullRequest | None:
    """Returns the PR or None. Used by intake to check if a webhook event
    matches a PR we already track."""
```

## What `domain/pull_requests` does NOT do

- Does not own `review_jobs` or the per-PR queue — that's `reviewer`.
- Does not own ticket state — that's `tickets`.
- Does not decide whether a PR should be reviewed — that's `intake`.
- Does not publish events. PR mirror updates are silent; user-visible state changes go through `tickets.TicketStatusChanged`.
- Does not enforce state-transition rules. The DB stores the state value; callers are trusted.
- Does not store outdated-comment markers. Force-push handling lives between `intake` and `vcs.mark_comments_outdated` (which is itself a no-op for GitHub since GitHub marks outdated automatically).
- Does not provide a list/filter API. The UI lists tickets, not PRs. Direct PR queries are by id or external id.

## Decisions

### 2026-05-14 — Two writer functions: `upsert` (metadata refresh) + `update_state` (explicit transitions)
Each does one thing. Audit entries differ (`pull_request.synced` vs `pull_request.state_changed`).

### 2026-05-14 — Silent module — no published events
`tickets.TicketStatusChanged` covers user-visible state changes. PR mirror updates don't surface in the UI directly.

### 2026-05-14 — No state-machine validation at this layer
The DB stores whatever the caller (intake) passes. Validation lives in intake (which decides the state from the GitHub event).
**Why:** PR state is a mirror, not a yaaof-controlled lifecycle. GitHub is the source of truth; we just copy.
