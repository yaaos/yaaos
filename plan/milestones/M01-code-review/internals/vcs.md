# `domain/vcs` — Internal Architecture

> Vendor-neutral abstraction over version-control providers (GitHub today, GitLab/Bitbucket/etc. eventually).
> Defines the types, Protocol, and registry that every VCS plugin implements and every consumer (`intake`, `pull_requests`, `reviewer`) depends on.

## Purpose

`domain/vcs` is the contract. It owns:

- The abstract data types yaaof uses to reason about pull requests, comments, diffs, reviews, findings.
- The `VCSPlugin` Protocol that every plugin implements.
- The discriminated union of inbound webhook events.
- The plugin registry.
- The typed exception hierarchy plugins raise.

It owns **zero** business logic. No filtering, no decisions, no LLM calls. Pure types and contracts.

## Public interface (`__all__`)

```python
# Types
"RepoRef", "VCSPullRequest", "Diff", "FileSummary", "Comment",
"Review", "Finding", "FindingSnippetLine", "Severity", "ReviewState", "ReviewPostResult",

# Events
"VCSEvent",
"PullRequestReadyForReview",
"PullRequestSynchronized",
"PullRequestClosed",
"PullRequestReopened",
"CommentCreated",
"ReactionAdded",

# Protocol — 9 methods (8 async + 1 sync)
"VCSPlugin",

# Registry
"register_vcs_plugin",
"get_plugin",
"get_plugin_for_repo",
"get_installation_token",
"PluginNotFoundError",

# Exceptions
"VCSError",
"VCSAuthError",
"VCSNotFoundError",
"VCSPermissionError",
"VCSRateLimitError",
"VCSTransientError",
"VCSValidationError",
```

## Type definitions

### Identity

- yaaof identifies every PR by an internal `UUID` ("yaaof PR ID"). The `pull_requests` table owns that UUID and stores `(plugin_id, external_id)` alongside.
- **Plugin methods never see yaaof UUIDs.** They take `external_id: str` (whatever opaque identifier the plugin uses internally — e.g., GitHub: `"owner/repo#123"`).
- Conversion happens at the call site (consumer queries `pull_requests` to map UUID → external_id).

```python
class RepoRef(BaseModel):
    """Minimal repo identity. Full Repo type lives in domain/repos."""
    plugin_id: str
    external_id: str
```

### `VCSPullRequest` — what plugins return

"Fat" type with all cheap PR metadata. **No yaaof UUID** — set when the PR is upserted into the `pull_requests` table; that upserted form is owned by `domain/pull_requests`.

```python
class VCSPullRequest(BaseModel):
    plugin_id: str
    external_id: str               # plugin-specific id ("owner/repo#123")
    repo_external_id: str          # plugin-specific repo id ("owner/repo")
    number: int                    # PR number on the VCS
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
    state: Literal["open", "closed", "merged"]
    html_url: str
    created_at: datetime
    updated_at: datetime
```

### `Diff` — `(raw, files)`

```python
class FileSummary(BaseModel):
    path: str
    status: Literal["added", "modified", "removed", "renamed"]
    old_path: str | None       # only for renames
    additions: int
    deletions: int

class Diff(BaseModel):
    raw: str                   # unified-diff text — what the LLM sees
    files: list[FileSummary]   # parsed sidecar — what the preprocessor reads
```

### `Comment` — yaaof-authored only

```python
class Comment(BaseModel):
    external_id: str           # the VCS's comment id (opaque string)
    body: str
    file_path: str | None      # None for top-level / review-body comments
    line: int | None
    posted_at: datetime
    in_reply_to_external_id: str | None
```

### `Review` and `Finding` (input to `post_review`)

```python
Severity = Literal["must-fix", "nit", "suggestion", "info"]
ReviewState = Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"]

class FindingSnippetLine(BaseModel):
    line_number: int           # source line number (same number repeats for the add/del pair)
    kind: Literal["context", "add", "del"]
    text: str                  # raw line content (no leading +/− prefix; UI renders it)

class Finding(BaseModel):
    file: str | None           # None ⇒ goes in review summary, not a line comment
    line_start: int | None
    line_end: int | None
    severity: Severity
    title: str                 # ≤120 chars
    body: str                  # markdown — the user-facing finding text
    rationale: str | None      # short justification rendered as a quoted block under body. Empty for trivial findings.
    snippet: list[FindingSnippetLine] | None    # optional suggested-change diff. None when no concrete code suggestion.
    applied_lesson_ids: list[UUID] = []          # lessons (by id) the agent attributes this finding to. UI renders chips linking to /memory.

class Review(BaseModel):
    agent_tag: str             # "architecture" | "security" | "style" — prefixed in comment bodies
    state: ReviewState
    summary_body: str | None   # top-level review body
    findings: list[Finding]

class ReviewPostResult(BaseModel):
    review_external_id: str
    # index in `Review.findings` → external comment id (for later outdated-marking)
    finding_to_comment_external_id: dict[int, str]
```

### `VCSEvent` — discriminated union (6 kinds)

Common fields on a base class; subclasses add `kind: Literal[...]` discriminator + kind-specific fields.

```python
class VCSEventBase(BaseModel):
    plugin_id: str
    source_event_id: str       # VCS's id, for idempotency
    received_at: datetime
    repo_external_id: str
    pr_external_id: str | None # None only for events not tied to a PR (none in M01)

class PullRequestReadyForReview(VCSEventBase):
    kind: Literal["pr_ready_for_review"]
    pr: VCSPullRequest

class PullRequestSynchronized(VCSEventBase):
    kind: Literal["pr_synchronized"]
    new_head_sha: str
    force_push: bool

class PullRequestClosed(VCSEventBase):
    kind: Literal["pr_closed"]
    merged: bool

class PullRequestReopened(VCSEventBase):
    kind: Literal["pr_reopened"]

class CommentCreated(VCSEventBase):
    kind: Literal["comment_created"]
    comment_external_id: str
    comment_kind: Literal["inline", "top_level"]
    body: str
    author_login: str
    author_type: Literal["user", "bot"]
    in_reply_to_comment_external_id: str | None

class ReactionAdded(VCSEventBase):
    kind: Literal["reaction_added"]
    target_comment_external_id: str
    reaction: Literal["thumbs_up", "thumbs_down"]
    actor_login: str

VCSEvent = Annotated[
    Union[
        PullRequestReadyForReview, PullRequestSynchronized, PullRequestClosed,
        PullRequestReopened, CommentCreated, ReactionAdded,
    ],
    Field(discriminator="kind"),
]
```

### Exceptions

```python
class VCSError(Exception): ...
class VCSAuthError(VCSError): ...           # 401/403, App uninstalled
class VCSNotFoundError(VCSError): ...       # 404
class VCSPermissionError(VCSError): ...     # 403 with specific message
class VCSRateLimitError(VCSError):
    retry_after: float | None
class VCSTransientError(VCSError): ...      # 5xx, network — retryable
class VCSValidationError(VCSError): ...     # 4xx other — usually a yaaof bug
class PluginNotFoundError(LookupError): ... # registry miss
```

## `VCSPlugin` Protocol

```python
class VCSPlugin(Protocol):
    meta: PluginMeta   # id="github", type="vcs", display_name="GitHub", …

    # Webhook reception is NOT on the Protocol — plugins register their own
    # webhook routes directly via core/webserver.register_routes(RouteSpec(...))
    # at import time (see plugins-github.md for the canonical example).
    # The plugin owns: signature verification, idempotency check (have we seen
    # this source_event_id?), parsing → list[VCSEvent], then handing those off
    # to intake.handle_vcs_events(events, *, org_id=...).
    #
    # Reason this isn't on the Protocol: webhook routing is a webserver concern,
    # not a VCS-vendor concern. Different VCS plugins may not even have webhooks
    # (a future poller-only plugin wouldn't). Forcing the Protocol method would
    # couple every plugin to the webhook model.

    # Read
    async def fetch_pr(self, external_id: str) -> VCSPullRequest: ...
    async def fetch_diff(self, external_id: str) -> Diff: ...
    async def list_yaaof_comments(self, external_id: str) -> list[Comment]: ...
    async def list_open_prs_since(
        self, repo_external_id: str, since: datetime
    ) -> list[VCSPullRequest]: ...
    async def is_repo_accessible(self, repo_external_id: str) -> bool: ...

    # Write
    async def post_review(
        self, external_id: str, review: Review
    ) -> ReviewPostResult: ...
    async def post_comment_reply(
        self, external_id: str, parent_comment_external_id: str, body: str
    ) -> str:
        """Post a reply in a comment thread. Returns the new comment's external_id.
        For GitHub: POST /repos/{owner}/{repo}/pulls/comments/{parent_id}/replies
        (for inline review-comment replies) or
        POST /repos/{owner}/{repo}/issues/{number}/comments
        with a quote-reference (for top-level comment replies).
        Plugin chooses the right endpoint based on parent comment type."""
    async def mark_comments_outdated(
        self, external_id: str, comment_external_ids: list[str]
    ) -> None: ...

    # Auth
    async def get_installation_token(self, org_id: UUID) -> str:
        """Return a freshly-issued installation token for the org. Caller uses
        it once (e.g., for a `git clone`) and forgets it. Tokens are short-lived
        (GitHub: ~1h); callers MUST NOT cache. Each call may mint a new token."""
```

Top-level dispatcher (alongside `get_plugin` / `get_plugin_for_repo`):

```python
async def get_installation_token(plugin_id: str, org_id: UUID) -> str:
    return await get_plugin(plugin_id).get_installation_token(org_id)
```

## Plugin registry

```python
# domain/vcs/registry.py
_PLUGINS: dict[str, VCSPlugin] = {}

def register_vcs_plugin(plugin: VCSPlugin) -> None:
    if plugin.plugin_id in _PLUGINS:
        raise ValueError(f"Plugin {plugin.plugin_id} already registered")
    _PLUGINS[plugin.plugin_id] = plugin

def get_plugin(plugin_id: str) -> VCSPlugin:
    try:
        return _PLUGINS[plugin_id]
    except KeyError:
        raise PluginNotFoundError(plugin_id) from None

def get_plugin_for_repo(repo: Repo) -> VCSPlugin:
    return get_plugin(repo.plugin_id)
```

Most call sites use `get_plugin_for_repo(repo)`. Webhook routing uses `get_plugin(plugin_id)` directly.

## Plugin lifecycle

- **One singleton per plugin per process.** Plugin is constructed at bootstrap (in `apps/backend/app/main.py`), registered into `_PLUGINS`.
- Plugin internally manages caches (e.g., GitHub installation token refresh).
- No per-request or per-org instances. When multi-org arrives, the plugin internally maintains per-installation state.

## Error contract

- Plugin methods raise `VCSError` subclasses on failure.
- **Consumers do not catch by default.** Exceptions propagate to:
  - The `core/primitives.spawn()` wrapper around the background coroutine (logs the failure; the coro itself is responsible for marking its domain row failed before the exception propagates).
  - The HTTP middleware (returns 500 JSON).
  - A thin retry wrapper at the plugin call site that retries `VCSTransientError` and `VCSRateLimitError` with backoff.
- See [../patterns.md](../patterns.md) Exceptions section.

## What `domain/vcs` does NOT do

- It does not decide which PRs to act on (that's `intake`).
- It does not maintain any state — no DB tables, no caches, no globals other than the registry dict.
- It does not perform HTTP calls — that's the plugin's job.
- It does not parse webhook signatures — that's the plugin's job.
- It does not know about LLMs, prompts, lessons, or any product concept beyond "PRs, diffs, comments, reviews."

## Open questions for implementation

- **Pagination of `list_yaaof_comments`.** For huge PRs with hundreds of yaaof comments, returning a single list may be expensive. M01: probably fine (small PR review counts); revisit if it bites.
- **`post_review` idempotency.** What if called twice (retry mid-flight)? GitHub's behavior: a second submit creates a second review. Plugin needs an idempotency key or precondition. Implementation detail; covered when writing the github plugin.
- **`list_open_prs_since` cursor model.** Timestamp-based (`updated_at > since`) vs sequential ID. Detail for github plugin.

## Decisions

### 2026-05-13 — Identity is yaaof UUID; plugin methods take `external_id` strings
Plugins never see yaaof UUIDs. Conversion at call site.
**Why:** keeps plugins ignorant of yaaof internals; simpler plugin code; future-proof against changes in yaaof's identity model.

### 2026-05-13 — `VCSPullRequest` is hybrid: fat for cheap metadata, methods for expensive things
Cheap fields embedded (title, author, sha, draft, is_fork, branches, urls); `Diff` and `list[Comment]` via plugin methods.

### 2026-05-13 — `Diff` is `(raw, files)`
Raw unified-diff for the LLM; lightweight file summaries for the preprocessor.

### 2026-05-13 — Plugin emits semantic events; intake filters
Plugins emit clean events (PR ready, synchronized, closed, etc.). Filtering rules (skip drafts/forks/bots) live in `intake`, not in plugins.

### 2026-05-13 — Singleton plugin per process
One instance per plugin, created at bootstrap. Plugin owns its own internal caches.

### 2026-05-15 — `Finding` carries optional `snippet`, `rationale`, and `applied_lesson_ids`
The coding-agent CLI is asked to produce these fields when relevant. `snippet` is a structured diff (list of `FindingSnippetLine`) so the UI can render line-numbered +/− blocks consistently; agents don't emit raw markdown code fences. `rationale` is a short justification rendered as a quoted block under the finding body. `applied_lesson_ids` lets the agent attribute a finding to specific lessons it consulted.
**Why:** the UI is the surface where reviewers learn yaaof's reasoning — surfacing why-this-finding and which-lesson-triggered-it makes the agent's output legible and gives users a concrete pivot to "teach yaaof" via the memory loop. Structured snippets (vs. raw markdown) keep rendering consistent across agents.

### 2026-05-16 — `get_installation_token(org_id)` added to the Protocol
Workspace plugins (e.g., `in_process_workspace.provision`) need VCS auth to `git clone`. They call `vcs.get_installation_token(plugin_id, org_id)` once per operation, use the token via `GIT_ASKPASS` (so it never appears in argv or persists to disk), then forget it.
**Why:** no long-lived credential anywhere — no internal HTTP endpoint, no cached secret, no daemon. M01 reviewer only needs auth at clone time. M02+ implementer workflows that run for hours use the same primitive: yaaof orchestrates each git operation with a fresh token; the workspace never stores credentials. Token theft surface is reduced to "exactly when an operation is running."

### 2026-05-15 — `post_comment_reply` added to the Protocol
Reviewer's targeted-reply workflow needs to post into a specific comment thread (not as a new top-level review). Plugin method posts a reply to a parent comment and returns the new comment's external_id. GitHub plugin picks the right API based on whether the parent is an inline review-comment or a top-level issue-comment.
