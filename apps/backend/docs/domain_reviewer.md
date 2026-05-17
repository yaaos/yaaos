# domain/reviewer

> Review workflow orchestrator — per-PR queue, review-job state machine, heartbeat, secrets pre-flight, frozen-snapshot audit, step-progress SSE, startup recovery.

## Purpose

Owns the `ReviewJob` aggregate (**one row per `(PR × review run)`** — no per-agent decomposition) and the lifecycle from "needs review" through workspace provisioning, coding-agent invocation, finding parsing, and posting. Does not call LLMs directly — `domain/coding_agent` plugins do. Subagent specialization happens inside the single coding-agent invocation (the parent dispatches yaaos-* subagents via the Task tool); the reviewer module sees one invocation per ticket.

Reply / verify-fix flows are deferred. A future `review_comments` table will own that lifecycle separately.

## Public interface

Exported from `app/domain/reviewer/__init__.py`:

- Types — `ReviewJob`, `ReviewJobInput`, `ReviewJobStatusChanged`, `ReviewJobRow`, `PostedCommentRow`.
- Scheduling — `schedule_review`, `cancel_pending`.
- Reads — `get_review_job`, `list_review_jobs_for_pr`, `list_in_flight`, `metrics_summary`.
- Lifecycle — `startup_recovery`.

HTTP routes (`/api/reviewer`):

- `POST /rereview` — body `{ ticket_id }`; UI button.
- `POST /cancel?ticket_id=...` — cancel queued/running job.
- `GET /jobs/by-ticket/{ticket_id}` — every review_job for the ticket's PR.
- `GET /metrics` — aggregate counters.

Route spec registers one `on_startup` hook: `startup_recovery`.

## Module architecture

### Files

- `models.py` — `ReviewJobRow`, `PostedCommentRow`.
- `queue.py` — events, audit payloads, `schedule_review`/`cancel_pending`, reads, `_run_review_job` (the per-ticket handler), transitions, secrets detection, language detect, `startup_recovery`.
- `web.py` — routes.

### Per-PR queue discipline

"At most one in-flight `ReviewJob` per PR" — enforced by service logic, not a unique index. `schedule_review` flips every `queued`/`running` row for the PR to `cancelled` with `skip_reason='superseded'`, writes `review_job.cancelled` audit, inserts the new `queued` row, spawns the handler.

### Cancellation — DB flip + task cancel

Two-track:

1. **DB-driven** — `cancel_pending` flips the row to `cancelled` and writes the `review_job.cancelled` audit. Always happens; what the UI reads.
2. **Task-driven** — `cancel_pending` also calls `asyncio.Task.cancel()` on the in-flight task (looked up in a module-level `_inflight_tasks` registry keyed by `review_job_id`). The cancellation propagates through `coding_agent.review` → `workspace.run_coding_agent_cli`, which catches `CancelledError`, kills the subprocess group (SIGTERM → 2s → SIGKILL), drains the pipes, and re-raises. The handler's outer `except asyncio.CancelledError` swallows the propagation (DB state is already terminal) and lets the cancellation finish unwinding.

Without the task-cancel half, the CLI would keep running until its own timeout (10 minutes default) even though the UI shows `cancelled`. Restart-survivability: `_inflight_tasks` is per-process; a task from a previous process is gone, but `cancel_pending` is a no-op for those (DB row is already cancelled, no live task to find).

### `schedule_review` — main entry point

Called by `intake` for `pr_ready`, `pr_synchronized`, `rereview_command`, and the UI's re-review button. Cancels any in-flight job for the PR, inserts ONE queued row, writes `review_job.scheduled` audit, publishes `ReviewJobStatusChanged(status="queued")`, spawns `_run_review_job`. Debounce from `core.config.Settings.yaaos_review_debounce_seconds` (30s prod, 0s tests).

### `_run_review_job` — per-ticket handler

Fire-and-forget coro spawned once per `schedule_review` call.

1. **Debounce sleep.** Bail if cancelled during the window.
2. **Flip to running.** `started_at`, `last_heartbeat_at`, `current_step='resolving_entities'`.
3. **Ticket-level resolution** — ticket, PR, vcs_plugin, lessons, diff, prior yaaos comments, vcs PR.
4. **`fetching_diff` step.**
5. **Ticket-level skip checks** — `_ticket_skip_reason(pr, diff)` returns `"fork"` / `"bot_author"` / `"trivial_diff"` / `"too_large"` / None. On hit, transition to skipped and return.
6. **Secrets pre-flight** — `_detect_secrets(diff)`. On match, post ONE refusal review, transition to `skipped(skip_reason="secrets_detected")`, return.
7. **Language detect.**
8. **`provisioning_workspace` step.** `with_workspace("in_process", ...)` for the ticket. Passes `pr.head_sha`, `pr.head_branch`, `pr.base_sha`, and `pr.base_branch` — the workspace fetches both the head and the PR's actual base branch (whatever it merges into, not always `main`) so the agent can `git diff base_sha..HEAD` itself.
9. **Build `ReviewContext`** — pr, diff, lessons, language_hint, prior_yaaos_comment_bodies. No persona, no agent_name — the parent reviewer's prompt and subagent definitions ship as files (see `plugins/claude_code`).
10. **Hash + snapshot** — `prompt_hash = sha256(ctx.model_dump_json())`, denormalize, write `review_job.prompt_sent` audit (hash, lesson IDs, checkout SHA, language hint).
11. **`invoking_agent` step.** `coding_agent.review(plugin_id="claude_code", workspace=ws, context=ctx)`.
12. **`posting_review` step.** Build `vcs.Review(agent_tag="yaaos", state, summary_body, findings)` and `vcs_plugin.post_review`. The github plugin uses each finding's `source_agent` for per-comment prefixes; the top-level body uses the review tag.
13. **Persist** — one `PostedCommentRow` per finding-that-became-a-comment; update row with `status='posted'`, telemetry, JSON findings.
14. **Audit + publish** — `review_job.posted` carries `findings_by_agent: {<source_agent>: count}`; `ReviewJobStatusChanged(status="posted")`.

Crashes are caught and converted to `failed` so the UI never shows forever-running.

### Step-progress SSE

`_set_step` writes `current_step` + `last_heartbeat_at` and publishes `ReviewJobStepProgress`. Frontend subscribes and invalidates the per-ticket query. Phases: `resolving_entities` → `fetching_diff` → `provisioning_workspace` → `invoking_agent` → `posting_review` → (`posted`|`failed`). Step changes generate no audit entries. Audit captures *scheduled*, *prompt_sent*, *posted*, *failed*, *skipped*, *cancelled*.

### Heartbeat

`last_heartbeat_at` is bumped on every `_set_step` — no separate heartbeat coroutine.

### Secrets pre-flight

Five regex rules catch high-confidence shapes: AWS access key, GitHub token, Anthropic key, OpenAI key, PEM private-key block. `_detect_secrets` scans only `+`-prefixed lines in `diff.raw` (excluding `+++` headers), returns the first matching rule id. Audit carries the rule id, never the matched bytes.

### Frozen-snapshot audit payload

`review_job.prompt_sent` carries the prompt hash, lesson IDs, checkout SHA, language hint. The subagent definitions are static files in `app/domain/coding_agent/reviewers/`; their content is captured by the prompt hash via the assembled review prompt.

### Denormalized fields on `review_jobs`

Beyond lifecycle: `prompt_hash`, `lessons_applied` (UUID[]), `tokens_in`, `tokens_out`, `cost_usd`, `duration_s`, `error_message`, `review_external_id`, JSON-dumped `findings` (each carrying its `source_agent`). Audit log remains historical truth.

### Caller + destination columns

- **`triggered_by`** — kickoff event. Values today: `pr_ready`, `pr_synchronized`, `rereview_command`, `ui_rereview`. Future: `implementer_loop`.
- **`destination`** — where findings went. `vcs` today. `caller` reserved for future `run_review` invocations that return findings without posting.

### Startup recovery

`startup_recovery` (`on_startup`): select `running` ids (crashed processes), flip to `failed` with `skip_reason='crashed'`, write `review_job.failed` per crashed id, select all `queued` and respawn one `_run_review_job` per row with zero debounce.

### In-flight tracking

`list_in_flight` returns `status in ('queued','running')`. No separate task registry, no broker. The domain row is the truth.

### Metrics

`metrics_summary` walks all rows once: `{review_jobs_by_status, total_reviews_posted, total_cost_usd, failure_count, failure_rate}`. Backs `GET /api/reviewer/metrics`.

## Data owned

- `review_jobs` — one row per `(PR × review run)`. Indexed on `(pr_id, status, created_at)` and `(status, last_heartbeat_at)`.
- `posted_comments` — one row per VCS comment yaaos has posted; PK `external_comment_id`. Read by `intake` to resolve "which review_job owns this comment".

Canonical schema in [core_database.md](core_database.md).

## How it's tested

`app/domain/reviewer/test/test_detect_secrets.py` exhaustively covers the pre-flight detector. Scheduling, supersession, the handler state machine, and startup recovery are covered by integration suites in `app/test/` and e2e tests in `apps/e2e/`.
