# domain/vcs

> Vendor-neutral abstraction over VCS providers — types, Protocol, registry, exception hierarchy.

## Purpose

The contract. Owns abstract data types for PRs, comments, diffs, reviews, and findings; the `VCSPlugin` Protocol; the discriminated union of inbound webhook events; the plugin registry; and the typed exception hierarchy. Owns **zero** business logic — no filtering, no decisions, no LLM calls, no HTTP. Pure types. Webhook routing is not on the Protocol; plugins register their own webhook routes via `core/webserver.register_routes`.

## Public interface

Exported from `app/domain/vcs/__init__.py`:

- **Types** — `RepoRef`, `VCSPullRequest`, `Diff`, `FileSummary`, `Comment`, `Review`, `Finding`, `FindingSnippetLine`, `ReviewPostResult`, `Severity`, `ReviewState`.
- **Events** — `VCSEvent`, `VCSEventBase`, `PullRequestReadyForReview`, `PullRequestSynchronized`, `PullRequestClosed`, `PullRequestReopened`, `CommentCreated`, `ReactionAdded`.
- **Protocol** — `VCSPlugin`.
- **Registry** — `register_vcs_plugin`, `unregister_vcs_plugin`, `scoped_vcs_plugin`, `get_plugin`, `is_registered`, `registered_plugin_ids`, `get_installation_token`. See [patterns.md § scoped_* context managers](patterns.md#scoped_-context-managers-for-import-time-registries).
- **Exceptions** — `VCSError`, `VCSAuthError`, `VCSNotFoundError`, `VCSPermissionError`, `VCSRateLimitError`, `VCSTransientError`, `VCSValidationError`, `PluginNotFoundError`.

No HTTP routes — type-only.

## Module architecture

### Identity model

Every PR has an internal `UUID` owned by `domain/pull_requests`. Plugin methods **never see yaaos UUIDs** — they take `external_id: str`, an opaque identifier the plugin defines (GitHub: `"owner/repo#123"`). Conversion happens at the call site. See [`/docs/glossary.md`](../../../docs/glossary.md).

### Type layout (`types.py`)

- `RepoRef` — minimal `(plugin_id, external_id)` repo identity.
- `VCSPullRequest` — "fat" PR snapshot with cheap metadata. Expensive things (diff, comments) come from plugin methods. No yaaos UUID.
- `Diff = (raw, files)` — `raw` is unified-diff text (LLM input); `files` is parsed `FileSummary` list (preprocessor input).
- `Comment` — yaaos-authored comment. Carries `external_id`, body, optional file/line, `in_reply_to_external_id`.
- `Finding` — single review finding with `severity`, `title`, `body`, optional `rationale`, optional structured `snippet`, and `applied_lesson_ids` for UI attribution chips.
- `Review` — wraps `state` (`APPROVED` / `CHANGES_REQUESTED` / `COMMENT`), optional `summary_body`, `agent_tag`, `list[Finding]`. Input to `post_review`.
- `ReviewPostResult` — returns `review_external_id` and `finding_to_comment_external_id` so callers can later mark comments outdated.

### Events

`VCSEvent` is a Pydantic discriminated union over six kinds: `pr_ready_for_review`, `pr_synchronized`, `pr_closed`, `pr_reopened`, `comment_created`, `reaction_added`. All share `VCSEventBase` (`plugin_id`, `source_event_id`, `received_at`, `repo_external_id`, optional `pr_external_id`); each subclass adds a `kind` literal and kind-specific fields. Plugins emit semantic events; filtering rules live in `intake`.

### `VCSPlugin` Protocol

A plugin exposes a `meta: PluginMeta` attribute plus async methods:

- Read: `fetch_pr`, `fetch_diff`, `list_yaaos_comments`, `is_repo_accessible`.
- Write: `post_review`, `post_comment_reply`, `mark_comments_outdated`.
- Auth: `get_installation_token(org_id)` — short-lived; callers use once (e.g., `git clone` via `GIT_ASKPASS`) and forget. Never cached.

### Registry (`registry.py`)

Process-global dict `_PLUGINS` keyed by `plugin.meta.id`. One singleton per plugin per process, constructed at bootstrap. `register_vcs_plugin` rejects duplicates; `unregister_vcs_plugin(plugin_id)` removes one entry (no-op if absent); `scoped_vcs_plugin(plugin)` is the test-safe context manager; `get_plugin` raises `PluginNotFoundError` on miss; `get_installation_token(plugin_id, org_id)` is the top-level dispatcher workspace plugins use.

### Exception contract

Plugin methods raise `VCSError` subclasses on failure. Consumers don't catch by default — exceptions propagate to the background-task wrapper (see [`patterns.md`](patterns.md)) or HTTP middleware ([`core_webserver.md`](core_webserver.md)). A thin retry wrapper at the plugin call site retries `VCSTransientError` and `VCSRateLimitError` with backoff. `VCSRateLimitError` carries optional `retry_after`.

## Data owned

None. Registry is in-memory. PR mirror state is in `domain/pull_requests`; comments and reviews are persisted by the VCS itself.

## How it's tested

`app/domain/vcs/test/`:

- `test_events_discriminator.py` — verifies `VCSEvent` round-trips through JSON via `TypeAdapter` for each kind.

Plugin behaviour (auth, parsing, posting) is exercised through each plugin's test suite under `app/plugins/<plugin>/test/`.
