# plugins/github

> Only place GitHub-specific code lives. Implements `domain/vcs.VCSPlugin` and owns `/api/github/`.

## Purpose

Bridges GitHub REST + webhooks to `domain/vcs` types. Implements every `VCSPlugin` method. Owns App authentication (RS256 JWT → installation token), the webhook receiver, manifest-flow + manual credential routes, installation/repositories endpoints for the Settings UI, a once-per-process startup catch-up poller, and four DB tables. No business logic about which PRs to act on — that belongs to `intake`.

## Public interface

- Singleton `GitHubPlugin` registered into `domain/vcs` at `bootstrap()`; also registers the `github_app_installed` onboarding contributor.
- Side-effect import of `web.py` wires HTTP routes (prefix `/api/github`):
  - `POST /webhook` — GitHub event receiver.
  - `POST /credentials` — manual operator entry (App ID, slug, PEM, webhook secret).
  - `GET /manifest-callback?code=...` — completes the GitHub App Manifest Flow.
  - `GET /installation` — three-state response driving the Settings UI.
  - `GET /repositories` — live list of repos the App sees.
  - `GET /health` — three-state credentials / installation / ok.
- `_start_catchup` is the `on_startup` hook on the github `RouteSpec`.
- Domain code never imports `plugins/github` directly — it goes through `domain/vcs`'s registry.

## Module architecture

### GitHub App authentication

Two-step (`service.py`):

1. **App JWT** (`_build_app_jwt`) — RS256-signed JWT, 9-minute window, signed via `pyjwt` with the stored PEM. If the PEM lacks a `BEGIN ... PRIVATE KEY` header (fake-github test sentinel), returns `jwt-fake-<app_id>` so test stacks stay offline.
2. **Installation token** (`_installation_token`) — `POST /app/installations/{id}/access_tokens`. ~1hr TTL. Re-acquired per call; no cache (one HTTP round-trip, M01 traffic doesn't warrant plumbing).

`get_installation_token(org_id)` is public Protocol because the workspace plugin calls it at clone time and forgets the token. Cross-module flow: `docs/system-architecture.md`.

### Credentials storage

Per-org row in `github_settings`: `app_id`, `slug`, `encrypted_private_key`, `encrypted_webhook_secret`. PEM + webhook secret Fernet-encrypted with `yaaos_encryption_key`. Decrypted on demand; nothing held in singleton state.

Two write paths converge on the same row:

- **Manifest flow** — Settings UI POSTs to `https://github.com/settings/apps/new`. GitHub redirects to `/manifest-callback?code=...`. Handler exchanges the code at `POST /app-manifests/{code}/conversions`, persists App ID + slug + PEM + webhook secret, then 303-redirects to `https://github.com/apps/{slug}/installations/new`. Errors redirect to `/settings?gh_manifest_error=...`.
- **Manual credentials** — operator pastes into the Settings form, POST `/credentials`. Validates PEM shape, then calls the same `set_github_credentials` writer.

### Webhook receiver (`POST /webhook`)

1. Read raw body (signature verification needs unaltered bytes).
2. Load `github_settings`. Missing → `400`.
3. Decrypt webhook secret. Verify `X-Hub-Signature-256` (`hmac.compare_digest`). Invalid → `400`.
4. Parse JSON. Resolve `org_id` (settings row's org; or installation row's org if `installation.id` matches).
5. **Idempotency** — `record_webhook_event` keyed on `X-GitHub-Delivery`. Duplicate → `200 {status: duplicate}`.
6. **Install lifecycle short-circuit** — `installation` events update `github_app_installations` directly via `upsert_installation` / `mark_installation_inactive`. They never flow through `intake` — infrastructure state, not domain events.
7. **Parse + enrich** — `parse_webhook` returns zero-or-more `VCSEvent`s. For `pull_request.synchronize`, handler does the force-push enrichment call and rebuilds events with the true flag.
8. **Dispatch** — `domain.intake.handle_vcs_events`. Failures logged; handler still responds `200` — GitHub doesn't retry and a 5xx would only mask the failure. Catch-up poller covers missed events on next startup.
9. `mark_webhook_processed(row_id)` stamps `processed_at`. Respond `200`.

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

Parser stays sync; webhook handler enriches afterwards.

### Force-push detection

For `pull_request.synchronize` only, handler calls `detect_force_push(repo, before_sha, after_sha)` → `GET /repos/{owner}/{repo}/compare/{before}...{after}`, returns `True` iff `status == "diverged"`. Handler `model_copy`s any `PullRequestSynchronized` events to inject the real flag. Keeps the parser pure.

### Installation route (`GET /installation`)

Three states:
1. No `github_settings` row — UI shows credentials form.
2. Credentials configured, no active install — UI shows install button pointing at `https://github.com/apps/{slug}/installations/new`.
3. Installed — UI shows "manage" link to `https://github.com/settings/installations/{external_id}` plus `account_login` and `installed_at`.

### Repositories proxy (`GET /repositories`)

Live passthrough to `GET /installation/repositories` with a fresh installation token. Returns `{repositories: [...], total_count}`. No yaaos-side allowlist — GitHub's installation picker is the source of truth. On failure: empty list plus human-readable `error`.

### Health route (`GET /health`)

Three-state: credentials missing / installed nowhere / ok. Standard `{healthy, message, checked_at}`. No outbound API call.

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
| `GET .../pulls?state=open` | `list_open_prs_since` / poller |
| `GET .../compare/{base}...{head}` | `detect_force_push`, `list_commit_messages` (the `commits[].commit.message` array; consumed by `incremental.py` to feed `TriggerInputs.new_commit_messages` for §7's base-merge heuristic) |
| `GET /installation/repositories` | repositories route + poller |
| `GET /repos/{owner}/{repo}` | `is_repo_accessible` |

`post_review` posts each finding as its own comment rather than bundling them into a single `Review` object — no top-level wrapper comment, no `APPROVE` / `REQUEST_CHANGES` verdict (deferred). Findings with `file` + `line_start` go to `POST /pulls/{n}/comments` (the inline-review-comments endpoint, which requires the PR's `head_sha` as `commit_id`); orphan findings and the secrets-warning `summary_body` case route to `POST /issues/{n}/comments` (GitHub's path for non-inline PR comments — naming aside, this is *not* a GitHub Issues operation). `Review.state` is recorded internally but ignored on post; the approve flow will reintroduce it later.

### Install ↔ org binding (M02)

The M02 `github_installations(installation_id PK, org_id, created_at)` table — owned by [`domain/identity`](domain_identity.md) — records which yaaos org an installation belongs to. Owners initiate the bind by hitting `GET /api/github/install` from `/orgs/<slug>/settings`; the handler signs `state={org_id}` via `itsdangerous.URLSafeTimedSerializer` (15-minute TTL, salt `yaaos-github-install`) and 302's to `https://github.com/apps/<slug>/installations/new?state=...`.

After the install completes, GitHub redirects to `GET /api/github/install_callback?installation_id=<n>&state=<signed>`. The handler verifies the signature + TTL and either inserts or updates the `github_installations` row to map `installation_id → org_id`. Bad/expired states return 400. Successful binds 303 to `/`.

`resolve_org_for_installation(installation_id)` is the public helper webhook consumers + the catch-up poller can call to look up which org owns an installation event. Returns `None` when the install hasn't been bound to an org yet.

### Catch-up poller

`_start_catchup` is the `on_startup` hook; spawns `run_catchup_loop()` via `core/observability.spawn("github.catchup", ...)`.

`run_catchup_loop`:
1. Sleep `yaaos_catchup_delay_seconds` (10s prod, 0s tests).
2. Load active `github_app_installations`.
3. Per distinct org, run `_run_catchup`.

`_run_catchup(org_id)`:
1. Find active install.
2. Issue installation token.
3. `GET /installation/repositories`.
4. Per repo, list open PRs, call `intake.refresh_pr_metadata(...)` — same upsert path as the webhook handler. New PRs get tickets; existing ones get title / body / sha updates. **Reviews are not replayed**; missed review-triggers during downtime are a known POC limitation.
5. Bump `github_poller_state.last_polled_at` per repo.

Per-repo / per-PR exceptions: log + continue. Loop runs once per process; doesn't re-arm.

## Data owned

All four tables detailed in `docs/architecture.md` under "Data model":

- `github_app_installations` — `status` is `active` / `suspended` / `uninstalled`.
- `github_settings` — App ID, slug, encrypted PEM + webhook secret (one per org).
- `github_webhook_events` — idempotency on `X-GitHub-Delivery`.
- `github_poller_state` — per-(org, repo) catch-up cursor.

Reads (M02): `github_installations` — owned by [`domain/identity`](domain_identity.md); the plugin only inserts via `/install_callback` and reads via `resolve_org_for_installation`.

## How it's tested

Unit tests in `app/plugins/github/test/`:

- `test_signature.py` — HMAC verification (valid, invalid, missing header, wrong prefix).
- `test_payload_parser.py` — every event-mapping branch.
- `test_post_review.py` — `post_review` routing (inline / orphan / summary-only / empty) and `_format_finding_body` rendering (agent emoji suffix, fallback, omitted-when-unset).

Full webhook + dispatch, manifest-callback, credentials, installation route, repositories proxy, force-push detection, and catch-up poller exercised end-to-end by `apps/e2e/` Playwright specs against `apps/fake-github`.
