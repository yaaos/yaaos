# `plugins/github` — Internal Architecture

> Concrete implementation of `domain/vcs`'s `VCSPlugin` Protocol for GitHub.
> The only place GitHub-specific code is allowed.

## Purpose

`plugins/github` is the bridge between GitHub's REST API + webhooks and yaaof's abstract `domain/vcs` types. It:

- Implements every method on `VCSPlugin` (`fetch_pr`, `fetch_diff`, `list_yaaof_comments`, `list_open_prs_since`, `is_repo_accessible`, `post_review`, `mark_comments_outdated`).
- Owns the HTTP webhook receiver, signature verification, idempotency check, and payload→`VCSEvent` translation.
- Manages GitHub App auth (JWT signing → installation-token acquisition → caching).
- Runs the catch-up poller on startup.
- Owns four DB tables: `github_app_installations`, `github_settings`, `github_webhook_events`, `github_poller_state`.

No business logic about *which* PRs to act on (that's `intake`). No knowledge of yaaof tickets, agents, or reviews beyond the abstract `vcs` types.

## Public interface (`__all__`)

```python
"GitHubPlugin",         # the VCSPlugin implementation (registered at bootstrap)
"GitHubAuthError",      # subclass of VCSAuthError for plugin-specific cases
```

Everything else is internal. Domain code never imports from `plugins/github`; it uses `domain/vcs`'s registry to get a `VCSPlugin` and never knows it's GitHub-specific.

## GitHub App authentication

GitHub Apps use a two-step auth:

1. **App JWT**: signed with the App's private RSA key, ~10min TTL. Identifies the App itself.
2. **Installation token**: obtained by calling `POST /app/installations/{installation_id}/access_tokens` with the App JWT. ~1hr TTL. Used for all repo-scoped API calls.

### Token caching strategy

In-memory only. The plugin singleton holds a dict:

```python
@dataclass
class _CachedToken:
    token: str
    expires_at: datetime
    installation_id: str

_tokens: dict[str, _CachedToken] = {}   # keyed by installation_id
```

Before every API call:
1. Look up the installation_id's cached token.
2. If missing or within 5min of expiry, acquire a fresh one via App JWT + `POST /app/installations/{id}/access_tokens`.
3. Use it.

Lost on process restart — next API call re-acquires. Acceptable POC trade-off; saves writing a token-refresh background job.

### App credentials

Stored in `github_settings` table (per-org row):

| Column | Source |
|---|---|
| `app_id` | GitHub-assigned numeric App id |
| `encrypted_private_key` | the PEM. Encrypted at rest with the boot-time encryption key from `core/config` |
| `encrypted_webhook_secret` | HMAC signing secret for webhook verification |

Decryption happens at plugin-bootstrap time; decrypted values are kept in plugin singleton state. The plugin re-reads the table if it can't find the credentials in memory (e.g., admin updated them mid-run).

## Webhook receiver

Single route, registered via `register_routes(RouteSpec(module_name="github", router=router, ...))` — applies the default `/api/github` prefix:

- `POST /api/github/webhook`

Flow on every request:

1. **Read raw body + headers**. FastAPI handler must `await request.body()` to get bytes (signature verification needs the unaltered bytes).
2. **Verify signature** against `X-Hub-Signature-256` header using `hmac.compare_digest`. Constant-time compare. If invalid: log + `400 Bad Request`. Do not insert into the idempotency table.
3. **Insert idempotency row** with `ON CONFLICT DO NOTHING`:
   ```sql
   INSERT INTO github_webhook_events (id, org_id, source_event_id, event_type, received_at, payload)
   VALUES (...)
   ON CONFLICT (source_event_id) DO NOTHING
   RETURNING id;
   ```
   If no row was inserted (already seen): respond `200 OK` immediately. No further processing.
4. **Parse payload** into a Pydantic model specific to the event type (e.g., `GitHubPullRequestPayload`).
5. **Translate** into a list of `VCSEvent` objects (see "Event mapping" below).
6. **Dispatch** each `VCSEvent` to `intake` via `intake.handle_vcs_events(events)`. (See `intake` internals doc.)
7. **Update `processed_at`** on the idempotency row.
8. Respond `200 OK`.

If steps 4–7 raise, the failure is logged structurally and an audit entry is written. The handler still responds `200 OK` — GitHub does not retry, and surfacing the failure as a 5xx would only mask it without recovery (the catch-up poller covers missed events on the next startup). The operator investigates manually via the audit log.

## Event mapping

GitHub webhook event types map to our `VCSEvent` types:

| GitHub event | Condition | Emits |
|---|---|---|
| `pull_request.opened` | `pull_request.draft == false` | `PullRequestReadyForReview` |
| `pull_request.opened` | `pull_request.draft == true` | (nothing — intake doesn't act on drafts) |
| `pull_request.ready_for_review` | always | `PullRequestReadyForReview` |
| `pull_request.synchronize` | always | `PullRequestSynchronized` (with computed `force_push`) |
| `pull_request.closed` | `merged == false` | `PullRequestClosed(merged=false)` |
| `pull_request.closed` | `merged == true` | `PullRequestClosed(merged=true)` |
| `pull_request.reopened` | always | `PullRequestReopened` |
| `pull_request.edited` | title or body changed | (PR-metadata sync only; no VCSEvent — see `intake`) |
| `issue_comment.created` | `issue.pull_request` set | `CommentCreated(kind="top_level")` |
| `pull_request_review_comment.created` | always | `CommentCreated(kind="inline")` |
| `reaction.created` | target is a yaaof-authored comment | `ReactionAdded` |
| everything else | — | ignored |

Per the [plugin-emits-semantic-events; intake-filters](vcs.md#decisions) rule: the plugin emits `PullRequestReadyForReview` only when the PR is *actually* ready (so draft-opened produces no event — drafts are not a yaaof concern at all), but it emits ready-for-review events even for bot-authored PRs with `author_type='bot'` populated, and lets `intake` filter on that field.

### Force-push detection

For `pull_request.synchronize` events, compute `force_push`:

```python
async def _detect_force_push(repo: str, before_sha: str, after_sha: str) -> bool:
    # GitHub: GET /repos/{owner}/{repo}/compare/{before}...{after}
    resp = await api.get(f"/repos/{repo}/compare/{before_sha}...{after_sha}")
    return resp.json()["status"] == "diverged"
```

Authoritative; extra API call per synchronize. Result populates `PullRequestSynchronized.force_push`.

## API client

`httpx.AsyncClient` instance held by the plugin singleton. One connection pool per process. The base URL is **`GITHUB_API_BASE_URL`** (defaults to `https://api.github.com`); tests override it to point at `apps/fake-github`. The plugin's REST + auth code paths are identical in test and production — the only difference is which host responds.

Endpoints used in M01:

| Endpoint | Use |
|---|---|
| `POST /app/installations/{id}/access_tokens` | Acquire installation token |
| `GET /repos/{owner}/{repo}/pulls/{number}` | `fetch_pr` |
| `GET /repos/{owner}/{repo}/pulls/{number}` (with `Accept: application/vnd.github.v3.diff`) | `fetch_diff` (raw diff) |
| `GET /repos/{owner}/{repo}/pulls/{number}` then parse `changed_files` from the JSON form | `fetch_diff` (file summaries) |
| `GET /repos/{owner}/{repo}/pulls/{number}/comments` | `list_yaaof_comments` (inline) |
| `GET /repos/{owner}/{repo}/issues/{number}/comments` | `list_yaaof_comments` (top-level) |
| `POST /repos/{owner}/{repo}/pulls/{number}/reviews` | `post_review` |
| `GET /repos/{owner}/{repo}/pulls?state=open` | catch-up poller |
| `GET /repos/{owner}/{repo}/compare/{base}...{head}` | force-push detection |
| `GET /app` | `health_check` (cheap; auth check) |

`list_yaaof_comments` filters by author = the App's bot user. The plugin caches the bot user's id on first call.

## Error mapping

GitHub responses map to `VCSError` subclasses:

| HTTP | Maps to |
|---|---|
| 401 | `VCSAuthError` |
| 403 (with rate-limit headers) | `VCSRateLimitError` (with `retry_after` from `X-RateLimit-Reset`) |
| 403 (otherwise) | `VCSPermissionError` |
| 404 | `VCSNotFoundError` |
| 422 | `VCSValidationError` |
| 429 | `VCSRateLimitError` |
| 5xx | `VCSTransientError` |
| Network errors (timeout, DNS) | `VCSTransientError` |

Plugin retries `VCSTransientError` + `VCSRateLimitError` internally (3 attempts, exponential backoff with jitter, respecting `Retry-After`). Other errors propagate immediately.

## Catch-up poller

On plugin bootstrap (after the FastAPI app starts):

```python
async def _run_catchup() -> None:
    for repo in active_repos():
        last_polled = (await get_poller_state(repo.id)).last_polled_at
        try:
            open_prs = await _list_open_prs(repo.external_id)
            for pr in open_prs:
                # Push through the same PR-metadata-sync path the webhook handler uses
                await intake.refresh_pr_metadata(repo.id, pr)
            await update_poller_state(repo.id, last_polled_at=now())
        except Exception:
            log.exception("catchup.failed", repo_id=repo.id)
            # Don't advance cursor; next startup retries
```

Started from FastAPI's `lifespan` via `core/primitives.spawn(name="github.catchup", coro=_catchup_then_idle())`. The coro `await asyncio.sleep(get_settings().catchup_delay_seconds)` first (lets the rest of the app finish initializing) — delay comes from `YAAOF_CATCHUP_DELAY_SECONDS` (default 10s in prod, 0s in tests) — then runs the catch-up logic once. It does not loop — re-syncs only happen at startup.

**No webhook-delivery replay in M01.** If yaaof was down and missed an event:
- A PR opened during downtime: caught by the poll (state refresh covers it).
- A commit pushed during downtime: ticket's PR metadata is refreshed, but the review trigger isn't replayed; the next real event will trigger a review. Document this as a known POC limitation.

## DB tables owned

All four are detailed in [../data-model.md](../data-model.md):

- `github_app_installations` — installation registry; status flag.
- `github_settings` — App id + encrypted credentials.
- `github_webhook_events` — idempotency.
- `github_poller_state` — per-repo catch-up cursor.

## Plugin lifecycle

- Singleton instantiated at bootstrap. Constructor reads `github_settings` (decrypts credentials).
- Registers itself into `domain/vcs`'s registry via `register_vcs_plugin(self)`.
- Registers webhook route via `core/webserver.register_routes(RouteSpec(...))`.
- Spawns `_catchup_then_idle` via `core/primitives.spawn` from FastAPI's `lifespan` (sleeps 10s, runs once).

## What `plugins/github` does NOT do

- Does not decide whether to review a PR — that's `intake`.
- Does not know about tickets, agents, lessons, or reviews — only `domain/vcs` types.
- Does not write to `audit_log` directly (consumers do — webhook receipt logs go through `intake`'s audit calls).
- Does not handle GitLab / Bitbucket / etc.
- Does not retry events beyond the in-plugin HTTP retry (no orchestration-level retry).

## Decisions

### 2026-05-14 — Installation tokens cached in-memory only
Lost on restart; re-acquired on next API call. Refresh ~5min before 1hr TTL expiry.
**Why:** POC simplicity; no token-refresh background job; the cost of re-acquiring is one HTTP call.

### 2026-05-14 — Force-push detection via GitHub `/compare` API
For `pull_request.synchronize` events, call `/compare/{before}...{after}`. If `status == "diverged"`, set `force_push=true`.
**Why:** authoritative. The "compare to stored head_sha" heuristic missed edge cases.

### 2026-05-14 — Catch-up poller refreshes open PRs; does NOT replay missed webhooks
On startup, list open PRs per repo and refresh their metadata. Don't try to replay individual missed webhook deliveries.
**Why:** POC simplicity. Webhook-redelivery API is more authoritative but adds complexity. The "missed-review-trigger" case is a known limitation; documented.

### 2026-05-14 — Webhook receiver responds 200 even on processing failure (after logging)
GitHub doesn't retry; we don't want GitHub to retry. Failures are surfaced via structured logs and (when caused inside `intake` or downstream) audit-log entries. Operator investigates manually.
**Why:** simpler than implementing retry-loop protection against poisonous events.

### 2026-05-14 — Use `httpx` directly; no `PyGithub` library
~9 endpoints; hand-rolled wrappers are smaller than a vendor SDK adapter. Keeps the dependency surface minimal.
