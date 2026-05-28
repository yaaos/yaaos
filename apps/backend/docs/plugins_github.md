# plugins/github

> Only place GitHub-specific code lives. Implements `domain/vcs.VCSPlugin`, owns `/api/github/`, and provides the GitHub user-auth `Provider`.

## Purpose

Bridges GitHub REST + webhooks to `domain/vcs` types. Two distinct GitHub registrations sit behind the plugin: a **GitHub App** drives per-org installs (`yaaos_github_app_*`); a separate **GitHub OAuth App** drives "Sign in with GitHub" (`yaaos_github_oauth_*`). They are different GitHub primitives — don't conflate them. No per-org credential storage.

Owns: App authentication (RS256 JWT → installation token), the webhook receiver, the user-auth `Provider`, installation/repositories endpoints for the Settings UI, and two DB tables.

## Public interface

- Singleton `GitHubPlugin` registered into `domain/vcs` at `bootstrap()`; also registers the `github_app_installed` onboarding contributor.
- `GitHubOAuthProvider` registered into `domain/identity` at `bootstrap_oauth()` when the OAuth App's `client_id` + `client_secret` are configured.
- `record_app_install(session, *, org_id, install_external_id, account_login, status="active") -> None` — shape (a) public primitive to insert a `github_app_installations` row. Callers compose it inside their own `db_session()` block. Use for admin-onboarding or e2e seeding where the installation is already known. See [patterns.md § Service-fn session-handling convention](patterns.md).
- Side-effect import of `web.py` wires HTTP routes (prefix `/api/github`):
  - `POST /webhook` — GitHub event receiver.
  - `GET /installation` — two-state response driving the Settings UI.
  - `POST /install/start` — owner-initiated install handshake (returns state-signed redirect URL).
  - `GET /install_callback` — post-install redirect target; writes the install row.
  - `GET /repositories` — live list of repos the App sees.
  - `GET /health` — `app_provisioned` / `installed` / `ok`.
- Domain code never imports `plugins/github` directly — it goes through `domain/vcs`'s registry and `domain/identity`'s provider registry.

## Module architecture

### Two GitHub registrations, two purposes

The plugin is fronted by two distinct GitHub-side registrations. GitHub names them confusingly — they are *not* the same primitive:

- **GitHub App** — used for per-org installs. Authenticates with an RS256-signed App JWT (`yaaos_github_app_private_key`) → short-lived installation tokens. Owns the webhook deliverability contract. No `client_id`/`client_secret`.
- **GitHub OAuth App** — used for "Sign in with GitHub". Authenticates with `client_id`/`client_secret` → a user access token. No install concept, no installation tokens, no webhooks.

Why two: the install lifecycle (org admins granting repo access) and the login flow (individual users authenticating) live on different GitHub primitives. Trying to reuse one for the other ties credential ownership to whatever team controls the App registration, and conflates two unrelated failure modes.

Env vars (see [docs/setup.md](../../../docs/setup.md)):

- `yaaos_github_app_id` — App's numeric id.
- `yaaos_github_app_slug` — used to build `${github_web_base_url}/apps/<slug>/installations/new`.
- `yaaos_github_app_private_key` — PEM, used for App-JWT minting.
- `yaaos_github_app_webhook_secret` — HMAC verification.
- `yaaos_github_oauth_client_id` / `_client_secret` — OAuth App credentials for sign-in.
- `yaaos_github_oauth_token_url` (optional) — server-side token-exchange URL override (test stack only; server can't reach the browser-facing host).

Install + sign-in are independent code paths and never share DB state.

### GitHub App authentication (server-to-GitHub)

Two-step (`service.py`):

1. **App JWT** (`_build_app_jwt`) — RS256-signed JWT, 9-minute window, signed via `pyjwt` with the platform PEM. If the PEM lacks a `BEGIN ... PRIVATE KEY` header (fake-github test sentinel), returns `jwt-fake-<app_id>` so test stacks stay offline.
2. **Installation token** (`_installation_token`) — `POST /app/installations/{id}/access_tokens`. ~1hr TTL. Re-acquired per call; no cache.

`_installation_token` looks the per-org `installation_id` up in `github_app_installations`; the credential source is `_platform_credentials()`, which reads env vars.

`get_installation_token(org_id)` is public Protocol because the workspace plugin calls it at clone time and forgets the token.

### Login provider (GitHub OAuth App)

`GitHubOAuthProvider` implements `domain/identity.Provider`, driven by the **OAuth App** credentials (`yaaos_github_oauth_client_id`/`_secret`), not the GitHub App's:

- `authorization_url()` builds `${github_web_base_url}/login/oauth/authorize?client_id=...&redirect_uri=...&state=...&allow_signup=false`. No `scope` param — OAuth App scopes are configured on the registration itself.
- `exchange_code()` POSTs to the token URL (`yaaos_github_oauth_token_url` if set, else `${github_web_base_url}/login/oauth/access_token`), then fetches `${github_api_base_url}/user` and `/user/emails`, returns a normalized `ProviderProfile` with `external_subject = user.id`, the verified primary email, and `provider_login = user.login` (the GitHub handle, which the orchestrator persists to `users.github_username`).

`mfa_satisfied=True` — GitHub's own 2FA check runs inside the authorize handshake; yaaos doesn't demand a separate TOTP step-up on top.

### Webhook receiver (`POST /webhook`)

1. Read raw body (signature verification needs unaltered bytes).
2. HMAC-verify `X-Hub-Signature-256` against `yaaos_github_app_webhook_secret`. Missing or invalid → `401`.
3. Parse JSON. Resolve `org_id` via `github_app_installations` lookup on `payload.installation.id`. `installation.created` events fall back to `DEFAULT_ORG_ID` (single-tenant); every other event rejects as `bad_request` when no install row matches.
4. **Idempotency** — `record_webhook_event` keyed on `X-GitHub-Delivery`. Duplicate → `IntakeSideEffect(detail="duplicate")`, endpoint commits a no-op and returns 200.
5. **Branch on event + action** inside `GithubIntakeType.handle()`:
   - `pull_request.opened|reopened|ready_for_review` → filter forks / bots / drafts (writing `webhook_event.filtered`); race-safe ticket+PR upsert; `engine.start("pr_review_v1", …)` — all on the endpoint's session, single transaction.
   - `pull_request.synchronize` → refresh PR metadata, call `reviewer.start_incremental_review`.
   - `pull_request.closed` → update PR state, complete ticket, cancel workflows.
   - `pull_request.reopened` → PR state → open.
   - `issue_comment.created` / `pull_request_review_comment.created` → parse yaaos command or route as developer reply.
   - `reaction.created` → audit row on the related ticket.
   - `installation.created|unsuspend|new_permissions_accepted` → `upsert_installation`.
   - `installation.deleted|suspend` → `mark_installation_inactive`.
6. `mark_webhook_processed(row_id)` stamps `processed_at`. Endpoint commits and returns 200.

### Event mapping (`payload_parser.parse_webhook`)

Pure-data translator — no I/O, no DB:

| GitHub event | Condition | Emits |
|---|---|---|
| `pull_request.opened` | `draft == false` | `PullRequestReadyForReview` |
| `pull_request.opened` | `draft == true` | (nothing) |
| `pull_request.ready_for_review` | always | `PullRequestReadyForReview` |
| `pull_request.synchronize` | always | `PullRequestSynchronized(prev_head_sha=payload.before, force_push=False)` (handler overwrites `force_push` after the compare-API enrichment) |
| `pull_request.closed` | always | `PullRequestClosed(merged=...)` |
| `pull_request.reopened` | always | `PullRequestReopened` |
| `issue_comment.created` | `issue.pull_request` set | `CommentCreated(kind="top_level")` |
| `pull_request_review_comment.created` | always | `CommentCreated(kind="inline")` |
| `reaction.created` | `+1` / `-1` | `ReactionAdded` |
| everything else | — | ignored |

### Force-push detection

For `pull_request.synchronize` only, handler calls `detect_force_push(repo, before_sha, after_sha)` → `GET /repos/{owner}/{repo}/compare/{before}...{after}`, returns `True` iff `status == "diverged"`. Handler `model_copy`s any `PullRequestSynchronized` events to inject the real flag.

### Installation route (`GET /installation`)

Owner/Admin only (`VCS_READ`). Two states:
1. `app_configured: false` — the platform GitHub App isn't provisioned on this deployment (`yaaos_github_app_id`/`_slug`/`_private_key` unset). UI shows operator guidance. Tracks the GitHub App only; OAuth App credentials are irrelevant here.
2. `installed: false` — App provisioned but not installed on this org. UI shows the "Install yaaos on GitHub" button (clicks fire `POST /install/start`).
3. `installed: true` — UI shows "Manage on GitHub" link to `${github_web_base_url}/settings/installations/{external_id}` plus `account_login` and `installed_at`.

The response intentionally does NOT include a raw github.com URL for installation — exposing one would let callers skip the state-signing step the callback relies on.

### Install start (`POST /install/start`)

Owner-only (`GITHUB_APP_LINK`). Returns `{redirect_url}` — `${github_web_base_url}/apps/${slug}/installations/new?state=<signed-org_id>`, where the slug comes from `yaaos_github_app_slug`. The SPA POSTs this (so the `X-Org-Slug` + `X-CSRF-Token` headers reach the auth chain) and then sets `window.location.href = redirect_url`. 409 `app_not_provisioned` when the slug is unset.

### Install callback (`GET /install_callback`)

GitHub redirects here with `installation_id=<n>&state=<signed>`. Handler verifies the signature + 15-minute TTL (salt `yaaos-github-install`), fetches `account.login` via `GET /app/installations/<id>` (App JWT), and upserts the row via `upsert_installation`. Going through the App API rather than waiting for the `installation.created` webhook means dev environments without a webhook tunnel still get a complete row. Bad/expired states return 400. First-bind writes an audit row + sets `orgs.vcs_plugin_id="github"` + `vcs_settings={installation_id}`. Successful binds 303 to `/`.

Webhook delivery later upserts the same row again with the same `account_login` + `status="active"`; both code paths converge on the same row keyed by `install_external_id`.

### Repositories proxy (`GET /repositories`)

Live passthrough to `GET /installation/repositories` with a fresh installation token. Returns `{repositories: [...], total_count}`. No yaaos-side allowlist — GitHub's installation picker is the source of truth. On failure: empty list plus human-readable `error`.

### Health route (`GET /health`)

Three-state: App not provisioned / installed nowhere / ok. Standard `{healthy, message, checked_at}`. No outbound API call.

### REST endpoints used

No shared `httpx.AsyncClient` — short-lived per-method against `github_api_base_url` (defaults to `https://api.github.com`; tests point at `apps/fake-github`). Same code path in test and prod.

| Endpoint | Used by |
|---|---|
| `POST /app/installations/{id}/access_tokens` | `_installation_token` |
| `GET /repos/{owner}/{repo}/pulls/{n}` | `fetch_pr` |
| `GET .../pulls/{n}` (diff Accept) + `/files` | `fetch_diff` |
| `GET .../pulls/{n}/comments`, `/issues/{n}/comments` | `list_yaaos_comments` |
| `POST .../pulls/{n}/comments` (inline) + `POST .../issues/{n}/comments` (top-level) | `post_review` |
| `POST .../pulls/{n}/comments/{id}/replies` (`/issues/{n}/comments` fallback on 404) | `post_comment_reply` |
| `GET .../compare/{base}...{head}` | `detect_force_push`, `list_commit_messages` |
| `GET /installation/repositories` | repositories route |
| `GET /repos/{owner}/{repo}` | `is_repo_accessible` |
| `GET /user`, `GET /user/emails` (with user-access token) | `GitHubOAuthProvider.exchange_code` |

`clone_url(repo_external_id)` returns `<github_web_base_url>/<owner>/<repo>.git`. The workspace provider pairs this with a fresh installation token (via `GIT_ASKPASS`) to clone. Keeping clone-URL shape inside the GitHub plugin means non-github VCS plugins don't need to teach the workspace provider about their URL conventions.

`post_review` posts each finding as its own comment rather than bundling them into a single `Review` object. Findings with `file` + `line_start` go to `POST /pulls/{n}/comments`; orphan findings and the secrets-warning `summary_body` case route to `POST /issues/{n}/comments`.

## Data owned

- `github_app_installations` — `(org_id, install_external_id, account_login, status)`. `status` is `active` / `suspended` / `uninstalled`. Single source of truth for install↔org bindings; written by both the install callback and the `installation.*` webhook.
- `github_webhook_events` — idempotency on `X-GitHub-Delivery`.

No per-org credential storage. The historical `github_settings` table was dropped in migration `030_drop_github_settings`.

## How it's tested

Unit tests in `app/plugins/github/test/`:

- `test_signature.py` — HMAC verification (valid, invalid, missing header, wrong prefix).
- `test_payload_parser.py` — every event-mapping branch.
- `test_post_review.py` — `post_review` routing (inline / orphan / summary-only / empty) and `_format_finding_body` rendering.
- `test_install_binding.py` — install start (state signing, role gate, slug 409), install callback (happy path, bad state, missing params), webhook signature scoping.

Full webhook + dispatch, login round-trip, install handshake, repositories proxy, and force-push detection exercised end-to-end by `apps/e2e/` Playwright specs against `apps/fake-github`.
