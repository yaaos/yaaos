# plugins/github

> Only place GitHub-specific code lives. Implements `core/vcs.VCSPlugin`, owns `/api/github/`, and provides the GitHub user-auth `Provider`.

## Scope

Bridges GitHub REST + webhooks to `core/vcs` types. Two distinct GitHub registrations: a **GitHub App** for per-org installs; a **GitHub OAuth App** for "Sign in with GitHub". They are different GitHub primitives — do not conflate them. No per-org credential storage.

## Module architecture

### Two GitHub registrations

- **GitHub App** — per-org installs. RS256 App JWT (`yaaos_github_app_private_key`) → short-lived installation tokens. Owns webhook delivery.
- **GitHub OAuth App** — "Sign in with GitHub". `client_id`/`client_secret` → user access token. No install concept, no webhooks.

Why two: install lifecycle and login flow live on different GitHub primitives; sharing one conflates two independent failure modes.

`bootstrap()` registers, at import time: the `VCSPlugin` (`register_vcs_plugin`), an onboarding contributor + VCS-clear hook (`domain/orgs`), three [`domain/actions`](domain_actions.md) `Action`s (`github:create_pr`, `github:update_pr`, `github:reply_to_comment` — see § Actions below), and three [`core/intake`](core_intake.md) `IntakePoint`s the Repos-page trigger picker lists — `github:pr_opened`, `github:pr_commits` (both consumed by the webhook rewire below), and `github:pr_comment` (the comment-response run target `domain/pr_review.maybe_start_batch_run` resolves).

Env vars: `yaaos_github_app_id`, `yaaos_github_app_slug`, `yaaos_github_app_private_key`, `yaaos_github_app_webhook_secret`, `yaaos_github_oauth_client_id`, `yaaos_github_oauth_client_secret`, `yaaos_github_oauth_token_url` (optional, test stack only). See [docs/setup.md](../../../docs/setup.md).

### App authentication (server-to-GitHub)

1. **App JWT** (`_build_app_jwt`) — RS256-signed, 9-minute window. Fake PEM sentinel returns `jwt-fake-<app_id>` for test stacks.
2. **Installation token** — `POST /app/installations/{id}/access_tokens`, ~1hr TTL, acquired per-call, no cache.

`get_installation_token(org_id)` is on the public Protocol because the workspace plugin needs it at clone time. `list_installation_repos(org_id)` is on the Protocol too: this plugin owns repo enumeration (`GET /installation/repositories`, `per_page=100`, full-names only; `[]` on missing install or error), and sibling plugins (claude_code) read it through the `core/vcs` registry rather than importing this plugin.

### Login provider (GitHub OAuth App)

`GitHubOAuthProvider` implements `core/identity.Provider`. `authorization_url()` builds the GitHub authorize URL, requesting `read:user user:email` — a classic OAuth App grants scopes at authorize time, and `/user/emails` 404s without `user:email`. `exchange_code()` POSTs to the token URL, fetches `/user` + `/user/emails`, returns a `ProviderProfile` with `external_subject = user.id`, verified primary email, and `provider_login = user.login` (persisted to `users.github_username`). `mfa_satisfied=True` — GitHub's own 2FA runs inside the authorize handshake.

### Webhook receiver (`POST /api/intake/github`, via `core/intake`)

`GithubIntakeType` (`intake_type.py`) is the actual receiver — registered with [`core/intake`](core_intake.md) as the `github` `IntakeType`; the endpoint is `core/intake`'s single `POST /api/intake/{type}`, not a route this plugin owns directly.

1. HMAC-verify `X-Hub-Signature-256`. Missing/invalid → `bad_signature` (401).
2. Resolve `org_id` via `github_app_installations` on `payload.installation.id`. `installation.created` falls back to `DEFAULT_ORG_ID`; other unmatched events reject as `bad_request`.
3. Idempotency on `X-GitHub-Delivery` (`github_webhook_events`) — duplicate → `IntakeSideEffect(detail="duplicate")`, 200 no-op.
4. Dispatch on event + action. `pull_request.opened`/`reopened`/`ready_for_review` (non-draft) resolve the `github:pr_opened` intake point against `domain/repos` trigger bindings: **bound** → ticket created (`branch_name` = the PR's own head branch) → `domain/pipelines.start_run` per binding; **unbound** → 2xx no-op, no ticket, no run. `pull_request.synchronize` refreshes PR metadata and, on a bound repo, also fires `github:pr_commits` on the same ticket. A `PipelineNotFoundError`/`PipelineValidationError` from `start_run` (a stale binding or flatten-time cycle — both config problems, not delivery problems) is recorded as a `ticket.pipeline_start_failed` audit row; the webhook response stays 2xx so GitHub never retries. See [domain_repos.md](domain_repos.md) + [domain_pipelines.md](domain_pipelines.md).
5. `issue_comment.created`/`pull_request_review_comment.created` (bot comments filtered first): every comment on a known ticket — `@yaaos` grammar and free text alike — routes to `domain/pr_review.handle_pr_comment`, regardless of whether the ticket has ever run a pipeline (`re-review`/`cancel` no-op gracefully when there's no bound pipeline / no current run). See [domain_pr_review.md](domain_pr_review.md).

The `payload_parser.parse_webhook` function below (VCSEvent emission) and its event-mapping table describe a separate, currently-uncalled code path — `GithubIntakeType` parses payloads directly via `_parse_pr`, not through `parse_webhook`.

### Event mapping (`payload_parser.parse_webhook`)

Pure-data; no I/O.

| GitHub event | Condition | Emits |
|---|---|---|
| `pull_request.opened` | `draft == false` | `PullRequestReadyForReview` |
| `pull_request.opened` | `draft == true` | (nothing) |
| `pull_request.ready_for_review` | always | `PullRequestReadyForReview` |
| `pull_request.synchronize` | always | `PullRequestSynchronized` |
| `pull_request.closed` | always | `PullRequestClosed` |
| `pull_request.reopened` | always | `PullRequestReopened` |
| `issue_comment.created` | `issue.pull_request` set | `CommentCreated(kind="top_level")` |
| `pull_request_review_comment.created` | always | `CommentCreated(kind="inline")` |
| `reaction.created` | `+1`/`-1` | `ReactionAdded` |
| everything else | — | ignored |

### Force-push detection

For `pull_request.synchronize` only: `GET /repos/{owner}/{repo}/compare/{before}...{after}` → `True` iff `status == "diverged"`. Handler injects the flag into `PullRequestSynchronized`.

### Install routes

- `GET /installation` — two-state (`app_configured: false` / `installed`). Never returns a raw github.com install URL; callers must go through `/install/start`.
- `POST /install/start` — owner-only (`GITHUB_APP_LINK`). Returns signed `redirect_url` to `github.com/apps/<slug>/installations/new`.
- `GET /install_callback` — verifies state signature + 15-min TTL, fetches `account.login`, upserts install row, 303 to `/`. Webhook delivery later upserts the same row; both converge on `install_external_id`. `upsert_installation` is atomically idempotent and returns whether this call inserted the row; the install-callback fires `github_app_installation_linked` audit + `set_vcs` exactly once per first bind.

### Other routes

- `GET /repositories` — live passthrough to `GET /installation/repositories`. No yaaos-side allowlist.
- `GET /health` — three-state: not provisioned / not installed / ok. No outbound API call.

### REST endpoints used

All calls use short-lived per-method `httpx` clients against `github_api_base_url` (defaults to `https://api.github.com`; tests point at `apps/fake-github`).

`post_finding` posts each finding as its own comment from named primitive args rendered by `_format_finding_body`. Findings with `file` + `line_start` → `POST /pulls/{n}/comments` (inline); null-anchor findings → `POST /issues/{n}/comments` (top-level PR comment). `post_comment` always routes to `POST /issues/{n}/comments` — used by the secrets-detected warning.

`clone_url(repo_external_id)` returns `<github_git_base_url>/<external_id>.git` (falls back to `github_web_base_url` when the git base is unset). Workspace provider pairs this with a fresh installation token via `GIT_ASKPASS`. The git base is split from the web base so the agent clones from a host its container can reach — the web base is browser-facing (OAuth redirects).

### PR write surface

- `create_pr` — `POST /pulls`; idempotent per head branch: on GitHub's 422 "PR already exists" response, falls back to `GET /pulls?head=<owner>:<branch>&state=open` and returns that PR's external id instead of erroring.
- `approve_pr` — `POST /pulls/{n}/reviews` with `event=APPROVE`; submitted as the app, never merges.
- `has_active_approval` — `GET /pulls/{n}/reviews`; reads the latest review by the app's own bot login (`<yaaos_github_app_slug>[bot]`) and returns whether its state is `APPROVED`. GitHub is the source of truth — no local marker.
- `resolve_finding_thread` — GitHub has no REST endpoint for resolving a review thread. Two GraphQL round trips against `POST /graphql`: a `reviewThreads` query to locate the thread id anchoring the given comment (`databaseId`), then the `resolveReviewThread` mutation.

### Actions (`actions.py`)

Three [`domain/actions.Action`](domain_actions.md)s, registered by `bootstrap()`:

- **`github:create_pr`** — opens the PR for a yaaos-authored ticket's work branch: `GitHubPlugin.get_default_branch` (a live `GET /repos/{owner}/{repo}` lookup, not part of the `VCSPlugin` Protocol — no stored config carries a repo's default branch elsewhere) supplies `base_branch`, `vcs.create_pr` opens it (idempotent on retry via its own find-existing-for-branch fallback), `tickets.upsert`/`attach_pr_to_ticket` bind it to the ticket. A ticket that already has a PR (an externally-authored review ticket whose definition opens with `create_pr` anyway) skips straight to posting.
- **`github:update_pr`** — reflects the preceding review stage's mechanically-applied verdicts onto the PR: resolves the thread of every `fixed` finding (`vcs.resolve_finding_thread`) and posts any verdict `reply` text into the finding's own thread (`vcs.post_comment_reply`). The engine already applied the status transition before this action runs (`domain/findings.resolve`/`reflag`/`reopen`) — this only makes GitHub reflect it.
- **`_post_residuals`** — the posting primitive both actions share: posts every not-yet-anchored `ActionContext.preceding_residuals` finding via `vcs.post_finding` and `findings.set_external_anchor`. Externally idempotent: before posting, it reconciles against `vcs.list_yaaos_comments` — a finding's own `handle` (e.g. `SPEC-001`) rides verbatim in the `rule_violated` argument (the one `post_finding` argument this call fully controls), so a literal substring match against already-posted comment bodies survives a mid-body crash (GitHub comment created, DB anchor write lost) without double-posting.
- **`github:reply_to_comment`** — the comment-response run's action. Posts every `ActionContext.preceding_verdicts` entry's `reply` into the finding's own thread when one has been posted (`finding.external_comment_id is not None`), and carries the deterministic dispute policy: cross-references `domain/pr_review.list_comments_for_run` for which findings a batched comment classified `dispute`, then — `status is None` + a reply → `domain/findings.mark_defended` (the one defense); already-defended + any non-`user_overrode` verdict → coerce `domain/findings.dismiss` (method `user_overrode`) plus a generic acceptance reply if the skill's own reply is empty. `user_overrode` itself is excluded from the coercion check because the engine has already mechanically dismissed it before this action runs.

## Data owned

- `github_app_installations` — `(org_id, install_external_id, account_login, status)`. Written by both install callback and `installation.*` webhooks.
- `github_webhook_events` — idempotency on `X-GitHub-Delivery`.

No per-org credential storage. `github_settings` was dropped in migration `030_drop_github_settings`.

## How it's tested

Unit tests in `app/plugins/github/test/`:
- `test_signature.py` — HMAC verification.
- `test_payload_parser.py` — every event-mapping branch.
- `test_post_review.py` — `post_finding` (inline vs null-anchor routing) and `post_comment` routing, and `_format_finding_body`.
- `test_install_binding.py` — install start, install callback, webhook signature scoping.
- `test_intake_producer_service.py` (`@pytest.mark.service`) — a bound-repo PR-opened webhook calls `create_from_pr` + `attach_pr_to_ticket`; ticket row created, `ticket.created` + `ticket.pr_bound` audit rows written, SSE + notifications outbox row enqueued.
- `test_intake_rewire_service.py` (`@pytest.mark.service`) — bound `pull_request.opened`/`synchronize` route into `pipelines.start_run` (ticket `branch_name` set from the PR's head branch); an unbound repo is a 2xx no-op with no ticket created; a `start_run` config-problem exception surfaces as a 2xx outcome plus a `ticket.pipeline_start_failed` audit row.
- `test_set_github_plugin_for_tests.py` — `set_github_plugin_for_tests` swaps and restores the singleton for the block.
- `test_pr_actions_service.py` (`@pytest.mark.service`, live `apps/fake-github`) — `GitHubCreatePRAction`/`_post_residuals` driven directly (entry point is `Action.execute`, owned by this module): `create_pr` idempotency on retry after a simulated crash that lost the `attach_pr_to_ticket` write (DB shows no PR, GitHub already has one — `vcs.create_pr`'s find-existing-for-branch fallback resolves the same PR); posting reconciliation after a simulated crash that lost a finding's `external_comment_id` write (the comment is real on GitHub — `vcs.list_yaaos_comments` + the finding's own `handle` find it, no duplicate post). The full-engine acceptance flow (residuals posted + incremental review resolves a thread) lives at `apps/backend/app/domain/pipelines/test/test_pr_actions_service.py` instead — its entry point is `pipelines.start_run`, owned by that module. `github:reply_to_comment`'s coverage lives at `apps/backend/app/domain/pr_review/test/` instead — its entry point is `pr_review.handle_pr_comment`, owned by that module (see [domain_pr_review.md](domain_pr_review.md)).

`set_github_plugin_for_tests` (exported from `app.plugins.github`) is the test seam for swapping the singleton `GitHubPlugin` instance. `get_plugin` is module-private (not in `__all__`); tests access the singleton via the context manager's yielded value.

The PR write surface (`create_pr`/`approve_pr`/`has_active_approval`/`resolve_finding_thread`) is covered by `app/core/vcs/test/test_write_ops_against_fake_github.py` — a live `apps/fake-github` subprocess round trip, since these operations are best proven against the fake's real REST + GraphQL shim rather than `httpx_mock`. The `fake_github_base_url` fixture spawning that subprocess lives in the top-level `apps/backend/conftest.py` (shared across every module's tests), not a module-local `conftest.py`.

Full webhook, login, install handshake, repositories proxy, and force-push detection exercised by `apps/e2e/` specs against `apps/fake-github`.
