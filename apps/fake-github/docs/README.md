# fake-github

> Peer Python service that fakes every GitHub endpoint yaaos's `plugins/github` calls. Test stack only.

## Purpose

Tests exercise the github plugin's real code paths — JWT signing, HMAC verification, REST round-trips — without hitting `api.github.com`. Minimal FastAPI service implementing every endpoint yaaos calls plus a `/__test/*` control surface for seeding and dispatching HMAC-signed webhooks. `GITHUB_API_BASE_URL` points yaaos here; plugin code is unchanged between prod and test.

Not a yaaos backend module — absent from `tach.toml`, the module map, layering rules. Peer service.

## GitHub-compatible endpoints

| Method + path | Behavior |
|---|---|
| `GET /app` | `{id, slug: "yaaos-test"}`. Health checks. |
| `POST /app/installations/{id}/access_tokens` | `{token: "ghs_fake_<id>_x", expires_at: <+1h>}`. |
| `GET /repos/{owner}/{repo}` | `{full_name, default_branch: "main"}`. |
| `GET /repos/{owner}/{repo}/pulls/{number}` | Seeded PR JSON. With `Accept: application/vnd.github.v3.diff`, returns raw diff. |
| `GET .../pulls/{number}/files` | Seeded file list. |
| `GET .../pulls?state=open&head=owner:branch` | Seeded + created PRs for the repo, optionally filtered by state and/or head branch. |
| `POST .../pulls` | Opens a PR (`create_pr`). Idempotent per real GitHub: a second create for a head branch with an existing open PR returns 422 Validation Failed — callers fall back to the `head` filter above to find it. |
| `GET .../pulls/{number}/comments` | Inline comments yaaos has posted (in-memory). |
| `GET .../issues/{number}/comments` | Top-level comments yaaos has posted. |
| `POST .../pulls/{number}/comments` | Records an inline review comment; also opens a review thread (`review_threads`) anchored to it. Returns `{id}`. |
| `POST .../pulls/{number}/comments/{parent}/replies` | Records an inline reply. |
| `POST .../issues/{number}/comments` | Records a top-level (non-inline) PR comment. |
| `POST .../pulls/{number}/reviews` | Submits a review (`approve_pr` uses `event="APPROVE"`); always attributed to `state.app_bot_login`. |
| `GET .../pulls/{number}/reviews` | Reviews submitted so far, oldest first — `has_active_approval` reads the latest one by the app's bot login. |
| `GET /installation/repositories` | Seeded repo list. Drives the catch-up poller and the Settings repo list. |
| `GET .../compare/{before}...{after}` | `{status: <seeded or "ahead">, commits: [{commit: {message}}, ...]}`. Force-push spec seeds `"diverged"`; incremental-trigger specs seed commit messages via `seed_compare_commits`. |
| `POST /graphql` | Minimal shim backing `resolve_finding_thread` — two operations, dispatched by string-matching the operation name in `query`: a `reviewThreads` query (lists a PR's threads + their comments' `databaseId`) and the `resolveReviewThread` mutation (marks a thread resolved). No general GraphQL engine. |

Bearer-protected endpoints accept any bearer — the fake validates only that one is present.

## Git HTTP smart protocol

`app/git_backend.py` serves real bare git repos (`acme/review-happy`, `acme/review-nonconforming`, `acme/review-agentfail`) over the git smart-HTTP protocol via `git http-backend` (CGI): `GET /{owner}/{repo}/info/refs` (discovery, both `git-upload-pack` and `git-receive-pack` services) + `POST /{owner}/{repo}/git-upload-pack` (clone/fetch) + `POST /{owner}/{repo}/git-receive-pack` (push). Every bare repo is created with `http.receivepack=true` so the agent's `git push origin HEAD` and yaaos's `PushBranch` command succeed. `GET /__test/git_head_sha/{owner}/{repo}` returns the current HEAD SHA for building PR payloads against real commits.

## Test-control endpoints

| Method + path | Behavior |
|---|---|
| `POST /__test/reset` | Clears in-memory state, re-seeds defaults (acme/web#1, acme/api#1, default repo list). Called by every spec in `beforeEach`. |
| `POST /__test/seed_pr` | `{owner, repo, number, pr}`. Auto-called by the e2e `dispatchWebhook` helper for `pull_request` events. |
| `POST /__test/seed_diff` | `{owner, repo, number, diff, files}`. |
| `POST /__test/seed_compare_status` | `{base_to_head, status}`. Force-push spec uses this to inject `"diverged"`. |
| `POST /__test/seed_compare_commits` | `{base_to_head, commits: ["msg1", ...]}`. Seeds the commit-message list returned by the compare API for that range. Used by reviewer specs exercising the §7 base-merge heuristic. |
| `POST /__test/dispatch_webhook` | `{event, payload, target_url, delivery_id?}`. HMAC-signs with the shared test webhook secret, POSTs to `target_url` with `X-Hub-Signature-256` + `X-GitHub-Event` + `X-GitHub-Delivery`. How specs simulate "a PR opened on GitHub." |
| `GET /__test/posted_comments` | What yaaos has POSTed (both inline review comments and top-level PR comments). Used for outbound-call assertions. |

## Auth

Two shared secrets in `app/test_secrets.py` (committed; obviously fake):
- `APP_ID` — App's numeric id.
- `WEBHOOK_SECRET` — yaaos's HMAC verification on inbound; signing on `/__test/dispatch_webhook` outbound. Override with `GITHUB_WEBHOOK_SECRET` (set in `docker-compose.test.yml`).

App private key is not real RSA. yaaos's `_build_app_jwt` detects the missing `BEGIN ... PRIVATE KEY` header and emits `jwt-fake-<app_id>`, which fake-github accepts. Production uses real RS256 via `pyjwt`.

## In-memory state

`app/state.py` singleton `FakeGitHubState`:
- `seeded_prs: dict[str, dict]` — `"owner/repo#number"` → PR JSON.
- `seeded_diffs: dict[str, str]` — same key → raw diff.
- `seeded_files: dict[str, list[dict]]` — same key → file summaries.
- `installation_repositories: list[dict]`.
- `compare_status: dict[str, str]` — `"before...after"` → status.
- `compare_commits: dict[str, list[str]]` — `"before...after"` → commit messages returned by `/compare`.
- `posted_comments` — what yaaos has POSTed (inline + top-level PR comments).
- `_next_comment_id` — auto-increment counter.
- `reviews: dict[str, list[dict]]` — `"owner/repo#number"` → reviews submitted, oldest first.
- `review_threads: dict[str, dict]` — GraphQL node id → `{pr_key, comment_ids, resolved}`; one thread per inline comment.
- `app_bot_login` — the `<app-slug>[bot]` login attributed to PRs/reviews created "as the app."
- `_next_pr_number`, `_next_review_id` — auto-increment counters.

`POST /__test/reset` clears everything and re-seeds defaults from `app/seeds.py`: PRs `acme/web#1`, `acme/api#1`; repo entries `acme/web`, `acme/api`.

## Running locally

`cd apps/fake-github && uv sync && uv run uvicorn app.main:app --port 8081`. Usually run via `docker-compose.test.yml` alongside the backend.

## Tech

- Python 3.14 + FastAPI.
- Own `pyproject.toml`; uv workspace member.
- Single-file `Dockerfile`. ~280 LOC. No DB; state in-memory.

## What fake-github does NOT do

- Doesn't verify the App JWT signature — any bearer prefix accepted.
- Doesn't model rate limits or HTTP errors beyond a few cases (e.g., 404 on missing PRs).
- Doesn't validate HMAC on inbound `/__test/*` — trust-by-deployment (reachable only inside the test stack network).
- Doesn't simulate webhook retries — `/__test/dispatch_webhook` is one-shot.
