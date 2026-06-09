# plugins/github

> Only place GitHub-specific code lives. Implements `core/vcs.VCSPlugin`, owns `/api/github/`, and provides the GitHub user-auth `Provider`.

## Scope

Bridges GitHub REST + webhooks to `core/vcs` types. Two distinct GitHub registrations: a **GitHub App** for per-org installs; a **GitHub OAuth App** for "Sign in with GitHub". They are different GitHub primitives — do not conflate them. No per-org credential storage.

## Module architecture

### Two GitHub registrations

- **GitHub App** — per-org installs. RS256 App JWT (`yaaos_github_app_private_key`) → short-lived installation tokens. Owns webhook delivery.
- **GitHub OAuth App** — "Sign in with GitHub". `client_id`/`client_secret` → user access token. No install concept, no webhooks.

Why two: install lifecycle and login flow live on different GitHub primitives; sharing one conflates two independent failure modes.

Env vars: `yaaos_github_app_id`, `yaaos_github_app_slug`, `yaaos_github_app_private_key`, `yaaos_github_app_webhook_secret`, `yaaos_github_oauth_client_id`, `yaaos_github_oauth_client_secret`, `yaaos_github_oauth_token_url` (optional, test stack only). See [docs/setup.md](../../../docs/setup.md).

### App authentication (server-to-GitHub)

1. **App JWT** (`_build_app_jwt`) — RS256-signed, 9-minute window. Fake PEM sentinel returns `jwt-fake-<app_id>` for test stacks.
2. **Installation token** — `POST /app/installations/{id}/access_tokens`, ~1hr TTL, acquired per-call, no cache.

`get_installation_token(org_id)` is on the public Protocol because the workspace plugin needs it at clone time. `list_installation_repos(org_id)` is on the Protocol too: this plugin owns repo enumeration (`GET /installation/repositories`, `per_page=100`, full-names only; `[]` on missing install or error), and sibling plugins (claude_code) read it through the `core/vcs` registry rather than importing this plugin.

### Login provider (GitHub OAuth App)

`GitHubOAuthProvider` implements `core/identity.Provider`. `authorization_url()` builds the GitHub authorize URL, requesting `read:user user:email` — a classic OAuth App grants scopes at authorize time, and `/user/emails` 404s without `user:email`. `exchange_code()` POSTs to the token URL, fetches `/user` + `/user/emails`, returns a `ProviderProfile` with `external_subject = user.id`, verified primary email, and `provider_login = user.login` (persisted to `users.github_username`). `mfa_satisfied=True` — GitHub's own 2FA runs inside the authorize handshake.

### Webhook receiver (`POST /webhook`)

1. HMAC-verify `X-Hub-Signature-256`. Missing/invalid → 401.
2. Resolve `org_id` via `github_app_installations` on `payload.installation.id`. `installation.created` falls back to `DEFAULT_ORG_ID`; other unmatched events reject as `bad_request`.
3. Idempotency on `X-GitHub-Delivery` — duplicate → 200 no-op.
4. Dispatch on event + action (see event table below). Stamps `webhook_event.processed_at` and returns 200.

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
- `GET /install_callback` — verifies state signature + 15-min TTL, fetches `account.login`, upserts install row, 303 to `/`. Webhook delivery later upserts the same row; both converge on `install_external_id`.

### Other routes

- `GET /repositories` — live passthrough to `GET /installation/repositories`. No yaaos-side allowlist.
- `GET /health` — three-state: not provisioned / not installed / ok. No outbound API call.

### REST endpoints used

All calls use short-lived per-method `httpx` clients against `github_api_base_url` (defaults to `https://api.github.com`; tests point at `apps/fake-github`).

`post_finding` posts each finding as its own comment from named primitive args rendered by `_format_finding_body`. Findings with `file` + `line_start` → `POST /pulls/{n}/comments` (inline); null-anchor findings → `POST /issues/{n}/comments` (top-level PR comment). `post_comment` always routes to `POST /issues/{n}/comments` — used by the secrets-detected warning.

`clone_url(repo_external_id)` returns `<github_git_base_url>/<external_id>.git` (falls back to `github_web_base_url` when the git base is unset). Workspace provider pairs this with a fresh installation token via `GIT_ASKPASS`. The git base is split from the web base so the agent clones from a host its container can reach — the web base is browser-facing (OAuth redirects).

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
- `test_intake_producer_service.py` (`@pytest.mark.service`) — `_prepare_pr_review` enqueues notifications outbox row and publishes SSE event.

Full webhook, login, install handshake, repositories proxy, and force-push detection exercised by `apps/e2e/` specs against `apps/fake-github`.
