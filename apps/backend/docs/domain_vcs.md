# domain/vcs

> Vendor-neutral abstraction over VCS providers — types, Protocol, registry, exception hierarchy.

## Scope

Owns: abstract data types (`VCSPullRequest`, `Diff`, `Comment`, `Finding`, `Review`, `ReviewPostResult`, `VCSEvent` discriminated union), `VCSPlugin` Protocol, plugin registry, typed exception hierarchy.

Does NOT own: business logic, filtering, LLM calls, HTTP, PR mirror state (`domain/pull_requests`). Webhook routing is not on the Protocol — plugins register their own routes via `core/webserver.register_routes`.

## Why / invariants

- **Plugin methods never see yaaos UUIDs.** They take `external_id: str` (GitHub: `"owner/repo#123"`). Conversion happens at the call site.
- **`get_installation_token` is short-lived; callers use once** (e.g., `git clone` via `GIT_ASKPASS`) and forget. Never cached.
- **Status-not-raise for transient errors:** a thin retry wrapper at the plugin call site retries `VCSTransientError` and `VCSRateLimitError` with backoff. Other `VCSError` subclasses propagate to the background-task wrapper or HTTP middleware.

## `VCSPlugin` Protocol

Signatures in `app/domain/vcs/types.py`:
- Read: `fetch_pr`, `fetch_diff`, `list_yaaos_comments`, `is_repo_accessible`.
- Write: `post_review`, `post_comment_reply`, `mark_comments_outdated`.
- Auth: `get_installation_token(org_id)`.

## Registry

`app/domain/vcs/registry.py` — process-global `_PLUGINS` dict, one singleton per plugin. `register_vcs_plugin` rejects duplicates; `scoped_vcs_plugin(plugin)` is the test-safe context manager. See [patterns.md § scoped_* context managers](patterns.md#scoped_-context-managers-for-import-time-registries).

## Events

`VCSEvent` — Pydantic discriminated union over `pr_ready_for_review`, `pr_synchronized`, `pr_closed`, `pr_reopened`, `comment_created`, `reaction_added`. Plugins emit semantic events; filtering rules live in `intake`.

## Data owned

None. Registry is in-memory. PR mirror state is in `domain/pull_requests`.

## How it's tested

`app/domain/vcs/test/test_events_discriminator.py` — `VCSEvent` round-trips via `TypeAdapter` for each kind. Plugin behaviour in `app/plugins/<plugin>/test/`.
