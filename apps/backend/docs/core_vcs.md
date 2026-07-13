# core/vcs

> Vendor-neutral abstraction over VCS providers — transport types, Protocol, registry, exception hierarchy. No finding taxonomy.

## Scope

Owns: abstract transport types (`VCSPullRequest`, `Diff`, `Comment`, `VCSEvent` discriminated union), `VCSPlugin` Protocol, plugin registry, typed exception hierarchy.

Does NOT own: finding taxonomy (lives in `domain/findings`), business logic, filtering, LLM calls, HTTP, PR mirror state (`domain/tickets` — `pull_requests` table). Webhook routing is not on the Protocol — plugins register their own routes via `core/webserver.register_routes`.

## Why / invariants

- **No finding value object crosses the boundary.** `post_finding` takes named primitive args; each plugin renders a platform-appropriate body. Finding taxonomy (severity, code location) lives entirely in `domain/findings`.
- **Every async method takes `org_id: UUID` as the first positional arg.** The plugin uses `org_id` to look up its installation credentials; `external_id: str` (GitHub: `"owner/repo#123"`) identifies the PR or repo within that installation. Conversion from internal IDs to `external_id` happens at the call site.
- **`get_installation_token` is short-lived; callers use once** (e.g., `git clone` via `GIT_ASKPASS`) and forget. Never cached.
- **Status-not-raise for transient errors:** a thin retry wrapper at the plugin call site retries `VCSTransientError` and `VCSRateLimitError` with backoff. Other `VCSError` subclasses propagate to the background-task wrapper or HTTP middleware.
- **Lives in `core/`** because finding taxonomy lives in `domain/findings` — this module is pure transport infrastructure, no business decisions. `plugins/github → core/vcs` and `domain/* → core/vcs` are both legal downward imports.

## `VCSPlugin` Protocol

Signatures in `app/core/vcs/types.py`:

- Read: `fetch_pr`, `fetch_diff`, `list_yaaos_comments`, `is_repo_accessible`.
- Write (findings): `post_finding(org_id, external_id, *, file, line_start, line_end, severity, category, confidence, finding_display_id, rationale, rule_violated, rule_source, suggested_fix) -> str` — posts one finding as a platform comment; returns the external comment id. When `file`/`line_start` are `None`, the plugin posts a top-level PR comment.
- Write (plain messages): `post_comment(org_id, external_id, *, body) -> str` — plain top-level PR comment for non-finding system messages (e.g., secrets-detected warning).
- Write (replies): `post_comment_reply(org_id, external_id, parent_comment_external_id, body) -> str` — posts a reply into an existing thread; returns the external comment id. Wired by `github:update_pr`/`github:reply_to_comment` (verdict replies) and `domain/pr_review`'s `CLASSIFY_COMMENT` (canned `unclear` clarification).
- Write (retained, unused): `mark_comments_outdated` — kept for a future follow-up flow; no domain logic wired.
- Write (PR lifecycle):
  - `create_pr(org_id, repo_external_id, *, head_branch, base_branch, title, body) -> str` — opens a PR, returns its external id. Idempotent per head branch: the github plugin treats GitHub's 422 "PR already exists" response as the idempotency signal and looks up the existing open PR instead of erroring.
  - `approve_pr(org_id, external_id) -> None` — submits an approving review as the app. Never merges.
  - `has_active_approval(org_id, external_id) -> bool` — does yaaos currently hold a non-dismissed approval? The provider is the source of truth (no local marker); the github plugin reads the latest review by the app's own bot login.
  - `resolve_finding_thread(org_id, external_id, comment_external_id) -> None` — resolves the review thread anchoring a posted finding comment. GitHub has no REST endpoint for this — the github plugin uses the GraphQL `resolveReviewThread` mutation, first querying `reviewThreads` to locate the thread id anchoring the given comment.
- Auth: `get_installation_token(org_id)`.
- Repo enumeration: `list_installation_repos(org_id) -> list[str]` — live repo full-names the org's install can see; the plugin resolves its own credentials. Sibling plugins read repo lists through this (via the registry), never by importing the VCS plugin. Returns `[]` when the install is absent or the call fails.

## Registry and dispatch helpers

`app/core/vcs/registry.py` — `VCSRegistry` holds the plugin map in a `ContextVar[VCSRegistry | None]` with `default=None`; `_get()` lazily creates the instance on first access per context. Production composition roots do nothing — the default instance materialises on first `register_vcs_plugin` call. Per-test isolation binds a fresh `.copy()` via `plugin_registries_isolation` in `app/testing/isolation.py`. `register_vcs_plugin` rejects duplicates. `set_vcs_for_tests(plugin=X)` is the context manager for ad-hoc per-test swaps (add/replace a plugin for the duration; restores the prior binding on exit).

**Typed dispatch helpers** — callers always use the module-level helpers exported from `core/vcs` rather than calling `get_plugin(id).method(...)` directly. Each async helper opens a `vcs.{plugin_id}.{op}` OTel span around the underlying plugin call so every VCS network hop appears as a named child span in the trace. Exceptions propagate unchanged; `start_as_current_span` automatically records the exception and sets `StatusCode.ERROR` on the span. Synchronous helpers (`install_url`, `validate_settings`, `clone_url`) have no span — they do no network I/O.

Exported helpers: `fetch_pr`, `fetch_diff`, `list_yaaos_comments`, `is_repo_accessible`, `detect_force_push`, `list_commit_messages`, `post_finding`, `post_comment`, `post_comment_reply`, `mark_comments_outdated`, `create_pr`, `approve_pr`, `has_active_approval`, `resolve_finding_thread`, `install_url`, `validate_settings`, `clone_url`, `get_installation_token`, `list_installation_repos`, `get_install_credentials`, `resolve_plugin_id_for_repo`, `registered_plugin_ids`.

**`resolve_plugin_id_for_repo(org_id, repo_external_id) -> str`** — shared plugin-ID resolution helper. Single-plugin fast path: if exactly one plugin is registered, returns it without an accessibility round-trip. Multi-plugin: iterates registered plugins and returns the first for which `is_repo_accessible` answers `True`; falls back to the first plugin when none match. Returns `""` when no plugins are registered. Used by `domain/tickets.create_from_manual` (so manual tickets always carry a non-empty `plugin_id` for provision) and by `domain/pipelines.scheduler_jobs._resolve_plugin_id` (schedule-tick plugin resolution).

**`get_install_credentials(plugin_id, org_id, repo_external_id) -> InstallCredentials`** — convenience helper that combines `clone_url` + `get_installation_token` into a single call. Returns a frozen `InstallCredentials` model (`clone_url: str`, `installation_token: SecretStr`). Raises `VcsInstallNotFound` (subclass of `VCSError + LookupError`) when the token call raises `VCSAuthError` (e.g., app uninstalled or org has no install row). Called by `ProvisionWorkspace.dispatch` at workspace-dispatch time.

## Events

`VCSEvent` — Pydantic discriminated union over `pr_ready_for_review`, `pr_synchronized`, `pr_closed`, `pr_reopened`, `comment_created`, `reaction_added`. Plugins emit semantic events; filtering rules live in `intake`.

## Data owned

None. Registry is in-memory. PR mirror state is in `domain/tickets` (`pull_requests` table).

## How it's tested

- `app/core/vcs/test/test_events_discriminator.py` — `VCSEvent` round-trips via `TypeAdapter` for each kind.
- `app/core/vcs/test/test_dispatch_spans_service.py` — two service tests (marked `@pytest.mark.service`): one verifies that a raising plugin produces a `vcs.{plugin_id}.post_finding` span with an `exception` event and `StatusCode.ERROR`; the other verifies that an httpx request made inside a plugin produces a child HTTP span via `HTTPXClientInstrumentor`.
- `app/core/vcs/test/test_write_ops_against_fake_github.py` — integration: round-trips `create_pr` → `has_active_approval` → `approve_pr` → `has_active_approval` → `resolve_finding_thread` (plus `create_pr` idempotency) through the dispatch wrappers against a live `apps/fake-github` subprocess (spawned per-test by the top-level `apps/backend/conftest.py`'s `fake_github_base_url` fixture — shared across every module's tests, not module-local); a second test proves `git push` over HTTP to the fake's clone URL succeeds.
- Plugin behaviour in `app/plugins/<plugin>/test/`.
