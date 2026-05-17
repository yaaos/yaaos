# domain/reviewer

> Review workflow orchestrator — reviewer agents, per-PR queue, review-job state machine, heartbeat, secrets pre-flight, frozen-snapshot audit, step-progress SSE, startup recovery.

## Purpose

The busiest backend module. Owns the `ReviewJob` aggregate (one row per `(PR, agent, scheduling event)`), the three built-in reviewer agents (architecture, security, style), and the lifecycle from "needs review" through workspace provisioning, coding-agent invocation, finding parsing, and posting. Does not call LLMs directly — Claude Code does, behind the `domain/coding_agent` Protocol. Owns scheduling, debouncing, cancellation, audit trail, and cooperative-cancellation runtime on `core/primitives.spawn`.

Each **ticket** gets one workspace; the agents on that ticket share it. The coordinator (`_run_ticket_review`) provisions once and gathers all per-agent invocations against the same checkout.

## Public interface

Exported from `app/domain/reviewer/__init__.py`:

- Types — `ReviewJob`, `ReviewJobInput`, `ReviewerAgent`, `ReviewJobStatusChanged`, `ReviewJobRow`, `PostedCommentRow`, `ReviewerAgentRow`, `AgentNotFoundError`.
- Scheduling — `schedule_review`, `schedule_reply`, `cancel_pending`.
- Reads — `get_review_job`, `list_review_jobs_for_pr`, `list_in_flight`, `metrics_summary`.
- Agents — `list_agents`, `get_agent_by_id`, `get_agent_by_name`, `update_agent_prompt`, `reset_agent_prompt`, `ensure_builtin_agents`.
- Lifecycle — `startup_recovery`.

HTTP routes (`/api/reviewer`):

- `GET /agents` — list the three reviewer agents.
- `PUT /agents/{name}/prompt` — body `{ prompt_text }`; 400 on empty.
- `POST /agents/{name}/reset_prompt` — restore built-in default.
- `POST /rereview` — body `{ ticket_id }`; UI button.
- `POST /cancel?ticket_id=...` — cancel queued/running jobs.
- `GET /jobs/by-ticket/{ticket_id}` — every review_job for the ticket's PR.
- `GET /metrics` — aggregate counters.

Route spec registers two `on_startup` hooks: `startup_recovery` and `_seed_builtin_agents`.

## Module architecture

### Files

- `models.py` — `ReviewerAgentRow`, `ReviewJobRow`, `PostedCommentRow`.
- `agent_crud.py` — `ReviewerAgent` view model + CRUD.
- `seeds.py` — `DEFAULT_PROMPTS` and `builtin_prompt(name)`.
- `queue.py` — events, audit payloads, `schedule_*`/`cancel_pending`, reads, `_run_ticket_review` (the per-ticket coordinator), `_invoke_one_agent` (per-agent work inside the shared workspace), `_run_reply_job`, transitions, secrets detection, language detect, `startup_recovery`.
- `web.py` — routes.

### Per-PR queue discipline

"At most one in-flight `ReviewJob` per `(pr_id, agent_id)`" — enforced by service logic, not a unique index. `schedule_review` flips every `queued`/`running` row for the pair to `cancelled` with `skip_reason='superseded'`, writes `review_job.cancelled` audit, inserts the new `queued` row, spawns the handler.

Cancellation is DB-driven and cooperative. No task IDs. The coro polls its row at three safe points — after debounce, after entity resolution, after workspace provisioning — and returns early when status flips off `queued`/`running`.

### `schedule_review` — main entry point

Called by `intake` for `pr_ready`, `pr_synchronized`, `rereview_command`, and the UI's re-review button. Accepts `agent_names="all"` (expands to the three names) or an explicit list. For each agent: cancels in-flight (same `(pr_id, agent_id)`), inserts a queued row, writes `review_job.scheduled` audit, publishes `ReviewJobStatusChanged(status="queued")`. After all rows are created, spawns **ONE** coordinator (`_run_ticket_review`) for the ticket — not one coro per agent. Debounce from `core.config.Settings.yaaos_review_debounce_seconds` (30s prod, 0s tests).

### `_run_ticket_review` — per-ticket coordinator

Fire-and-forget coro spawned once per `schedule_review` call. Owns the ticket-scoped workspace lifecycle and dispatches every agent in parallel against it.

1. **Debounce sleep.**
2. **Drop superseded** — re-read the passed job_ids; any not still `queued` were cancelled/superseded during debounce and are dropped from `pending`. If none remain, return.
3. **Flip all pending to running** — single batched UPDATE; `started_at`, `last_heartbeat_at`, `current_step='resolving_entities'`.
4. **Ticket-level resolution (once)** — ticket, PR, vcs_plugin, lessons, diff, prior yaaos comments, vcs PR.
5. **Step progress per job** — `_set_step("fetching_diff", ...)` for each pending job so the UI shows activity.
6. **Ticket-level skip checks** — `_ticket_skip_reason(pr, diff)` returns `"fork"` / `"bot_author"` / `"trivial_diff"` / `"too_large"` / None. On hit, transitions every pending job to skipped with that reason and returns. These predicates don't vary by agent.
7. **Secrets pre-flight (once)** — `_detect_secrets(diff)`. On match, post ONE refusal review (tagged with the first agent's name), transition every pending job to `skipped(skip_reason="secrets_detected")`, return. (Previously this posted once per agent — fixed by the move to ticket scope.)
8. **Language detect (once)** — `_detect_language(diff)`.
9. **Build `_SharedReviewContext`** — value object capturing pr, diff, lessons, prior_bodies, vcs_pr, language. Reused for every agent's `ReviewContext`.
10. **Step progress per job** — `_set_step("provisioning_workspace", ...)`.
11. **Provision workspace** — `with_workspace("in_process", ...)` ONCE for the ticket.
12. **Parallel agent invocation** — `await asyncio.gather(*[_invoke_one_agent(workspace=ws, job_id=..., ctx=ctx, org_id=org_id) for job in pending])`. Per-agent failures don't fail the gather; the workspace closes after every gather'd coro returns.

Uncaught exceptions in the coordinator log `ticket_review.coordinator_crashed` and mark any still-`running` rows as failed so the UI doesn't show forever-running.

### `_invoke_one_agent` — per-agent work in shared workspace

Each parallel task does:

1. **Cancel check** — bail if the row is no longer `running`.
2. **Build per-agent `ReviewContext`** — reuses `ctx`'s diff/lessons/prior_bodies; layers in this agent's persona + agent_config.
3. **Hash + snapshot** — `prompt_hash = sha256(ctx.model_dump_json())`, denormalize hash + `lessons_applied`, write `review_job.prompt_sent` with frozen `_AgentSnapshot`, hash, lesson IDs, checkout SHA, language hint.
4. **Final cancel check** before the expensive CLI call.
5. **Step progress** — `_set_step("invoking_agent", ...)`.
6. **Invoke** — `coding_agent.review(plugin_id, workspace, context)`.
7. **Post result** — on `SUCCESS`: build `vcs.Review`, call `post_review`, write one `PostedCommentRow` per finding-that-became-a-comment, update row (`status='posted'`, telemetry, JSON findings); write `review_job.posted` audit; publish status change. Non-success → `_transition_failed`.

Uncaught exceptions log `invoke_one_agent.crashed` and convert to `failed`. One failing agent doesn't affect the others — they keep running.

### Concurrency safety in the shared workspace

M01 reviewer agents are read-only against the workspace checkout. Three concurrent CLI subprocesses sharing one working directory is safe as long as no agent writes there. When M02+ implementer agents land and need to write, the workspace Protocol gains claim/release semantics; the shared model stays — only the synchronisation surface grows.

### Step-progress SSE

`_set_step` writes `current_step` + `last_heartbeat_at` and publishes `ReviewJobStepProgress`. Frontend subscribes and invalidates the per-ticket query. Phases: `resolving_entities` → `fetching_diff` → `provisioning_workspace` → `invoking_agent` → `posting_review` → (`posted`|`failed`). Step changes generate no audit entries. Audit captures *scheduled*, *prompt_sent*, *posted*, *failed*, *skipped*, *cancelled*, *reply_posted*.

### Heartbeat

`last_heartbeat_at` is bumped on every `_set_step` — no separate heartbeat coroutine. The admin Activity page and stuck-job detection both read it.

### Secrets pre-flight

Five regex rules catch high-confidence shapes: AWS access key, GitHub token, Anthropic key, OpenAI key, PEM private-key block. `_detect_secrets` scans only `+`-prefixed lines in `diff.raw` (excluding `+++` headers), returns the first matching rule id. On match: ONE refusal review posted by the coordinator, every pending job transitions to `skipped(skip_reason="secrets_detected")`. Audit carries the rule id, never the matched bytes.

### Reply workflow

`schedule_reply` is lighter. Creates a `kind='reply'` row with `parent_comment_external_id` and `reply_body`, spawns `_run_reply_job` with zero debounce, supersedes any in-flight reply for the same triple. The handler builds a `ReplyContext`, provisions a workspace, calls `coding_agent.reply`, posts via `vcs_plugin.post_comment_reply` (not a top-level review). No `posted_comments` row. Audit kind `review_job.reply_posted`.

### Frozen-snapshot audit payload

`review_job.prompt_sent` carries a full `_AgentSnapshot` (id, name, prompt_text, plugin id, agent_config), prompt hash, lesson IDs, checkout SHA, language hint. Immutable — later prompt edits don't rewrite history.

### Denormalized fields on `review_jobs`

Beyond lifecycle: `prompt_hash`, `lessons_applied` (UUID[]), `tokens_in`, `tokens_out`, `cost_usd`, `duration_s`, `error_message`, `review_external_id`, JSON-dumped `findings`. Convenience views — audit log remains historical truth.

### Agent CRUD + seeding

`ensure_builtin_agents` (idempotent, `on_startup`) inserts missing rows from `DEFAULT_PROMPTS` with `coding_agent_plugin_id="claude_code"`, empty `agent_config`, `is_built_in=True`. `update_agent_prompt` and `reset_agent_prompt` write `reviewer_agent.prompt_updated` audit with `{prior_hash, new_hash, restored_to_default?}` — text not stored in audit.

### Startup recovery

`startup_recovery` (`on_startup`): select `running` ids (crashed processes), flip to `failed` with `skip_reason='crashed'`, select all `queued`. Writes `review_job.failed` per crashed id. Groups queued rows by `ticket_id` (resolved via the PR row) and respawns ONE `_run_ticket_review` per ticket with zero debounce — preserving the ticket-scoped workspace discipline. `queued` rows auto-resume; `failed` requires operator re-review.

### In-flight tracking

`list_in_flight` returns `status in ('queued','running')`. No separate task registry, no broker. The domain row is the truth.

### Metrics

`metrics_summary` walks all rows once: `{review_jobs_by_status, total_reviews_posted, total_cost_usd, failure_count, failure_rate}`. Backs `GET /api/reviewer/metrics`.

## Data owned

- `reviewer_agents` — one row per agent. Unique on `(org_id, name)`.
- `review_jobs` — one row per `(PR, agent, scheduling event)`. Indexed on `(pr_id, status, created_at)` and `(status, last_heartbeat_at)`.
- `posted_comments` — one row per VCS comment yaaos has posted; PK `external_comment_id`. Read by `intake` to resolve "which agent owns this comment".

Canonical schema in [core_database.md](core_database.md).

## How it's tested

`app/domain/reviewer/test/test_detect_secrets.py` exhaustively covers the pre-flight detector. Scheduling, supersession, the handler state machine, replies, and startup recovery are covered by integration suites in `app/test/` and e2e tests in `apps/e2e/`.
