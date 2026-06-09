# core/vcs

> Vendor-neutral abstraction over VCS providers — transport types, Protocol, registry, exception hierarchy. No finding taxonomy.

## Scope

Owns: abstract transport types (`VCSPullRequest`, `Diff`, `Comment`, `VCSEvent` discriminated union), `VCSPlugin` Protocol, plugin registry, typed exception hierarchy.

Does NOT own: finding taxonomy (lives in `domain/reviewer`), business logic, filtering, LLM calls, HTTP, PR mirror state (`domain/tickets` — `pull_requests` table). Webhook routing is not on the Protocol — plugins register their own routes via `core/webserver.register_routes`.

## Why / invariants

- **No finding value object crosses the boundary.** `post_finding` takes named primitive args; each plugin renders a platform-appropriate body. Finding taxonomy (severity, confidence, category) lives entirely in `domain/reviewer`.
- **Plugin methods never see yaaos UUIDs.** They take `external_id: str` (GitHub: `"owner/repo#123"`). Conversion happens at the call site.
- **`get_installation_token` is short-lived; callers use once** (e.g., `git clone` via `GIT_ASKPASS`) and forget. Never cached.
- **Status-not-raise for transient errors:** a thin retry wrapper at the plugin call site retries `VCSTransientError` and `VCSRateLimitError` with backoff. Other `VCSError` subclasses propagate to the background-task wrapper or HTTP middleware.
- **Lives in `core/`** because after finding taxonomy moved to `domain/reviewer`, the module is pure transport infrastructure — no business decisions. `plugins/github → core/vcs` and `domain/* → core/vcs` are both legal downward imports.

## `VCSPlugin` Protocol

Signatures in `app/core/vcs/types.py`:

- Read: `fetch_pr`, `fetch_diff`, `list_yaaos_comments`, `is_repo_accessible`.
- Write (findings): `post_finding(external_id, *, file, line_start, line_end, severity, category, confidence, finding_display_id, rationale, rule_violated, rule_source, suggested_fix) -> str` — posts one finding as a platform comment; returns the external comment id. When `file`/`line_start` are `None`, the plugin posts a top-level PR comment.
- Write (plain messages): `post_comment(external_id, *, body) -> str` — plain top-level PR comment for non-finding system messages (e.g., secrets-detected warning).
- Write (retained, unused): `post_comment_reply`, `mark_comments_outdated` — kept for future follow-up flows; no domain logic wired.
- Auth: `get_installation_token(org_id)`.
- Repo enumeration: `list_installation_repos(org_id) -> list[str]` — live repo full-names the org's install can see; the plugin resolves its own credentials. Sibling plugins read repo lists through this (via the registry), never by importing the VCS plugin. Returns `[]` when the install is absent or the call fails.

## Registry

`app/core/vcs/registry.py` — `VCSRegistry` holds the plugin map; the live instance is held in a `ContextVar` (`_registry_var`). A module-level `_default_registry` captures all import-time `bootstrap()` calls — production never calls `bind_vcs_registry()`. Per-test isolation binds a fresh `.copy()` of the session-scoped canonical snapshot via `plugin_registries_isolation` in `app/testing/isolation.py`. `register_vcs_plugin` rejects duplicates. Module-level dispatchers `get_installation_token(plugin_id, org_id)` and `list_installation_repos(plugin_id, org_id)` resolve the plugin by id and delegate — the seam sibling plugins use instead of importing the VCS plugin. `scoped_vcs_plugin(plugin)` in `app/testing/isolation` is the context manager for ad-hoc per-test swaps — it binds a fresh copy with the plugin replaced and restores the prior binding on exit.

## Events

`VCSEvent` — Pydantic discriminated union over `pr_ready_for_review`, `pr_synchronized`, `pr_closed`, `pr_reopened`, `comment_created`, `reaction_added`. Plugins emit semantic events; filtering rules live in `intake`.

## Data owned

None. Registry is in-memory. PR mirror state is in `domain/tickets` (`pull_requests` table).

## How it's tested

`app/core/vcs/test/test_events_discriminator.py` — `VCSEvent` round-trips via `TypeAdapter` for each kind. Plugin behaviour in `app/plugins/<plugin>/test/`.
