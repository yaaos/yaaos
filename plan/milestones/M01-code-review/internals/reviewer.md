# `domain/reviewer` — Internal Architecture

> The review workflow orchestrator. Biggest module by surface; coordinates everything from ticket → workspace → coding agent → findings → posted review.

## Purpose

`domain/reviewer` owns:

- The `ReviewJob` aggregate (`review_jobs` table) — one row per (PR, agent) review attempt.
- The `PerPRQueueDiscipline` — "at most one in-flight ReviewJob per (PR, agent)" enforced by service logic, not DB constraint.
- The review workflow: workspace creation, prompt assembly, agent invocation, finding parsing, verdict computation, GitHub posting.
- The targeted-reply workflow for human replies to specific agent comments.
- The `posted_comments` table (writes on every successful post; read by `intake` for reply-agent lookups).
- Startup recovery of crashed review_jobs.

It does NOT own: LLM calls (none — Claude Code does its own), lessons (`memory`), VCS state (`pull_requests`), workspace lifecycle (`core/workspace`). It DOES own its own agent definitions (the `reviewer_agents` table; see "Agent CRUD" below).

## Public interface (`__all__`)

```python
# Types
"ReviewJob",
"ReviewJobStatus",   # queued / running / posted / failed / skipped / cancelled
"SkipReason",        # draft / fork / bot_author / trivial_diff / too_large / crashed

# Public API
"schedule_review",
"schedule_reply",
"cancel_pending",

# Read API (for the UI)
"get_review_job",
"list_review_jobs_for_pr",
"list_findings_for_review_job",
"list_in_flight",
```

## Tables owned

Three tables, all detailed in [data-model.md](../data-model.md):

- `reviewer_agents` — one row per reviewer agent (M01: 3 hardcoded rows). Holds the prompt + plugin id + agent_config.
- `review_jobs` — one row per (PR, agent, scheduling event).
- `posted_comments` — one row per GitHub comment yaaof has posted.

## Public functions

### `schedule_review(ticket_id, *, agent_names, trigger_reason, actor)`

```python
async def schedule_review(
    ticket_id: UUID,
    *,
    agent_names: Literal["all"] | list[str],
    trigger_reason: str,         # "pr_ready" / "pr_synchronized" / "rereview_command" / "ui_button"
    actor: Actor,
    org_id: UUID,
) -> list[UUID]:
    """Schedule a (re-)review of the ticket's PR by the specified agents.

    For each target agent:
      1. PerPRQueueDiscipline: find any in-flight ReviewJob for (pr_id, agent_id) — mark
         it cancelled in DB (status='cancelled', skip_reason='superseded'). If the row
         was 'queued', the debounce-sleeping coro will exit at its next state check;
         if 'running', it will exit at the next safe-point poll inside the handler.
      2. Create a new review_job row (status='queued').
      3. Spawn the handler via `core.primitives.spawn(name=..., coro=_run_review_job(...))`.
         The handler `await asyncio.sleep(debounce_seconds)` first (the duration
         comes from `core/config.Settings.review_debounce_seconds`; default 30s
         in prod, 0s in tests), then re-reads its
         own row to decide whether to proceed. No task IDs are tracked — cancellation
         is DB-driven (set state; coro polls).
      4. Write audit_for_review_job(kind='review_job.scheduled', payload={...},
         actor=actor).

    Returns the list of newly-created review_job ids."""
```

Called by `intake` for: PR ready-for-review, PR synchronized, `@yaaof rereview` command. Also called by the UI's "Re-review" button (via an API endpoint owned by reviewer).

### `schedule_reply(ticket_id, agent_id, parent_comment_external_id, reply_body, actor)`

```python
async def schedule_reply(
    ticket_id: UUID,
    agent_id: UUID,
    parent_comment_external_id: str,
    reply_body: str,
    *,
    actor: Actor,
    org_id: UUID,
) -> UUID:
    """Schedule a targeted reply: the named agent responds to the specific thread.

    Different from schedule_review — this is a lighter invocation:
      - Creates a review_job with kind='reply' (extra column).
      - The handler builds a reply-specific prompt: the agent's original comment +
        human reply + diff for context. Asks for a single response message,
        not a findings list. Output schema is ReplyResponse { body: str }.
      - The agent's response is posted as a follow-up comment in the SAME thread
        on GitHub (not a new review).
      - No debounce (replies are atomic; the handler runs immediately after `spawn`).
      - PerPRQueueDiscipline applies: a reply supersedes any queued/running reply
        for the same (pr, agent, parent_comment_id). Full review jobs are unaffected.

    Returns the new review_job id."""
```

Called by `intake.handle_comment_created` when a human replies to a yaaof comment.

### `cancel_pending(ticket_id, *, actor)`

```python
async def cancel_pending(
    ticket_id: UUID,
    *,
    actor: Actor,
    org_id: UUID,
) -> int:
    """Cancel every queued or running review_job for the PR.

    For each affected row:
      - Status flips to 'cancelled' with skip_reason='ticket_closed'
        (or 'repo_removed' if called from the repo-removal flow).
      - The corresponding background coro polls its row at safe points; on seeing
        a non-running/non-queued status, it returns early. No explicit task
        cancellation is needed (the coro will exit cooperatively at its next check).

    Returns count cancelled.

    Called by intake.handle_pr_closed (PR merged/closed) and the future repo-removed
    flow."""
```

### Read API

```python
async def get_review_job(review_job_id, *, org_id) -> ReviewJob
async def list_review_jobs_for_pr(pr_id, *, org_id) -> list[ReviewJob]
async def list_findings_for_review_job(review_job_id, *, org_id) -> list[Finding]
async def list_in_flight(*, org_id) -> list[ReviewJob]:
    """Returns all review_jobs with status in ('queued', 'running').
    Backs the admin/ops view of currently-running agent work and is the read side
    of yaaof's in-flight tracking (no separate task registry — domain rows are
    the truth)."""
```

The UI's ticket-detail page reads via the first three for the "Agents" tab; an admin "Activity" page (or future status endpoint `/api/reviewer/jobs/in-flight`) uses `list_in_flight`.

### Agent CRUD (the `reviewer_agents` table)

```python
class ReviewerAgent(AgentSpec):
    """Extends domain.coding_agent.AgentSpec with persistence fields."""
    id: UUID
    org_id: UUID
    is_built_in: bool
    created_at: datetime
    updated_at: datetime

async def list_agents(*, org_id: UUID) -> list[ReviewerAgent]:
    """Returns all 3 reviewer agents (architecture, security, style)."""

async def get_agent_by_name(name: str, *, org_id: UUID) -> ReviewerAgent:
    """Raises AgentNotFoundError. Used by the review workflow."""

async def update_agent_prompt(
    name: str,
    new_prompt_text: str,
    *,
    actor: Actor,
    org_id: UUID,
) -> ReviewerAgent:
    """Validates non-empty; writes audit_for_reviewer_agent(kind='reviewer_agent.prompt_updated',
    payload={prior_hash, new_hash}, actor). Updates row in place."""

async def reset_agent_prompt(
    name: str,
    *,
    actor: Actor,
    org_id: UUID,
) -> ReviewerAgent:
    """Restores the built-in default prompt for the agent. Same audit entry kind,
    payload includes {restored_to_default: true}."""
```

### Seeding (first-migration)

The 3 reviewer agents are seeded by an Alembic data migration on first deploy. Default prompts are checked-in constants (in `domain/reviewer/seeds.py`). Each row: `name`, default `prompt_text`, `coding_agent_plugin_id='claude_code'`, `agent_config={}`, `is_built_in=true`.

The migration is idempotent: if the row exists (by `(org_id, name)`), it's left alone. Lets ops re-run migrations safely without overwriting customized prompts.

### HTTP routes (registered via core/webserver)

```
GET    /api/reviewer/agents                       → list_agents
GET    /api/reviewer/agents/{name}                → get_agent_by_name
PUT    /api/reviewer/agents/{name}/prompt          → update_agent_prompt (body: { prompt_text })
POST   /api/reviewer/agents/{name}/reset_prompt    → reset_agent_prompt
```

The FE `prompts` module talks to these. Validation: empty `prompt_text` → 400 with a field-keyed error map (e.g., `{"prompt_text": "must not be empty"}`). A reviewer-local `_validate_prompt_text` helper handles the check.

## `ReviewJobInput` — handler input

```python
class ReviewJobInput(BaseModel):
    """Everything _run_review_job / _run_reply_job needs to do its work.
    Captured at spawn time so the running coro doesn't need to re-resolve
    anything from the row beyond the per-poll status check."""
    review_job_id: UUID
    ticket_id: UUID
    agent_id: UUID
    org_id: UUID
    debounce_seconds: int                 # set from core/config.Settings.review_debounce_seconds at schedule time; 0 for replies and for startup-recovery respawns
    kind: Literal["review", "reply"] = "review"
    # Reply-only: which agent comment we're responding to
    parent_comment_external_id: str | None = None
    reply_body: str | None = None       # the human's reply text we're responding to
```

## The handler: `_run_review_job(input: ReviewJobInput)`

The background coroutine spawned by `schedule_review`. It owns the debounce sleep, the safe-point cancellation checks, and the heartbeat loop. It does the actual work.

Flow:

```python
async def _run_review_job(input: ReviewJobInput) -> None:
    org_id = input.org_id
    job_id = input.review_job_id

    # 0. Debounce. Duration comes from core/config.Settings.review_debounce_seconds
    #    (env var YAAOF_REVIEW_DEBOUNCE_SECONDS; default 30 in prod, 0 in tests).
    await asyncio.sleep(input.debounce_seconds)

    # 1. Bail-check: was this job cancelled while we were sleeping?
    job = await get_review_job(job_id, org_id=org_id)
    if job.status != "queued":
        return  # already cancelled / superseded / etc.

    # 2. Flip to running (sets started_at; heartbeat begins)
    await _transition_to_running(job_id, org_id=org_id)
    heartbeat = asyncio.create_task(_heartbeat_loop(job_id, org_id))

    try:
        # 3. Resolve referenced entities
        ticket = await tickets.get(job.ticket_id, org_id=org_id)
        pr = await pull_requests.get(ticket.pr_id, org_id=org_id)
        repo = await repos.get(pr.repo_id, org_id=org_id)
        agent = await _get_agent_by_id(job.agent_id, org_id=org_id)   # same module — reviewer owns reviewer_agents
        lessons = await memory.list_for_repo(repo.id, org_id=org_id)
        diff = await vcs.fetch_diff(pr.plugin_id, pr.external_id)
        prior_yaaof_comments = await vcs.list_yaaof_comments(pr.plugin_id, pr.external_id)

        # 4. Skip checks (might have changed since scheduling)
        if _should_skip(pr, diff, repo):
            skip_reason = _compute_skip_reason(pr, diff, repo)
            await _transition_to_skipped(job_id, skip_reason, org_id=org_id)
            return

        # 5. Pre-flight: secrets scan (per requirements)
        if _detect_secrets(diff):
            await vcs.post_review(pr.plugin_id, pr.external_id, _secrets_warning_review())
            await _transition_to_skipped(job_id, "secrets_detected", org_id=org_id)
            return

        # 6. Language detect (or use repo.language_hint)
        language = repo.language_hint or _detect_language(diff)

        # 7. Build the review context — the plugin owns prompt assembly.
        review_ctx = ReviewContext(
            persona=agent.prompt_text,
            agent_name=agent.name,
            pr=vcs_pr,
            diff=diff,
            lessons=lessons,
            language_hint=language,
            prior_yaaof_comment_bodies=[c.body for c in prior_yaaof_comments],
            agent_config=agent.agent_config,
        )

        # 8. Hash the context (everything that influences the agent's output) and
        #    snapshot lesson IDs onto the row for UI read-speed.
        prompt_hash = hashlib.sha256(review_ctx.model_dump_json().encode()).hexdigest()
        await _denormalize_run_snapshot(
            job_id,
            prompt_hash=prompt_hash,
            lessons_applied=[l.id for l in lessons],
            org_id=org_id,
        )

        # 9. Write the frozen-snapshot audit entry
        await audit_log.audit_for_review_job(
            job_id, kind='review_job.prompt_sent',
            payload=FrozenSnapshotPayload(
                agent=agent.model_dump(),
                prompt_hash=prompt_hash,
                lessons_count=len(lessons),
                checkout_sha=pr.head_sha,
            ),
            actor=Actor(kind='system'),
            org_id=org_id,
        )

        # 9. Create workspace + invoke agent
        async with core.workspace.with_workspace(
            provider_id="in_process",
            spec=WorkspaceSpec(
                repo=RepoRef(plugin_id=pr.plugin_id, external_id=repo.external_id),
                sha=pr.head_sha,
                branch_name=pr.head_branch,
                resource_caps=ResourceCaps(),
                network_policy=NetworkPolicy.GITHUB_ONLY,
            ),
        ) as ws:
            # Final bail-check before the expensive call
            job = await get_review_job(job_id, org_id=org_id)
            if job.status != "running":
                return  # cancelled while workspace was being provisioned

            result = await coding_agent.review(
                plugin_id=agent.coding_agent_plugin_id,
                workspace=ws,
                context=review_ctx,
            )

        # 10. Handle the result by status. `result.findings` are already vcs.Finding;
        #     `result.state` is the verdict (APPROVED / CHANGES_REQUESTED / COMMENT)
        #     computed by the plugin. No DTO conversion in reviewer.
        if result.status == "success":
            post_result = await vcs.post_review(
                pr.plugin_id,
                pr.external_id,
                Review(
                    agent_tag=agent.name,
                    state=result.state or "COMMENT",
                    summary_body=result.summary_body,
                    findings=result.findings,
                ),
            )
            await _write_posted_comments(job_id, agent.id, pr.id, post_result, org_id=org_id)
            await _transition_to_posted(job_id, post_result.review_external_id, org_id=org_id)
            await audit_log.audit_for_review_job(
                job_id, kind='review_job.posted',
                payload=ReviewPostedPayload(
                    verdict=result.state or "COMMENT",
                    finding_count=len(result.findings),
                    tokens_in=result.telemetry.tokens_in,
                    tokens_out=result.telemetry.tokens_out,
                    cost_usd=result.telemetry.cost_usd,
                    latency_ms=result.telemetry.latency_ms,
                ),
                actor=Actor(kind='agent', agent_id=agent.id),
                org_id=org_id,
            )
        elif result.status in ("parse_failure", "agent_error", "timeout"):
            await _transition_to_failed(job_id, error=result.error_message, org_id=org_id)
            await audit_log.audit_for_review_job(
                job_id, kind='review_job.failed',
                payload=ReviewFailedPayload(
                    invocation_status=result.status,
                    error=result.error_message,
                    raw_output_excerpt=result.telemetry.raw_output[:1000],   # truncate for storage
                ),
                actor=Actor(kind='system'),
                org_id=org_id,
            )

    except Exception as e:
        # Infrastructure failure (DB error, plugin not found, etc.)
        log.exception("review_job.handler_crashed", review_job_id=job_id)
        await _transition_to_failed(job_id, error=f"handler crashed: {e}", org_id=org_id)
        # No re-raise: this is a fire-and-forget coro spawned via core/primitives.spawn,
        # which already attaches its own structured log + span around the coroutine.
    finally:
        heartbeat.cancel()
```

### Heartbeat loop

```python
async def _heartbeat_loop(job_id: UUID, org_id: UUID) -> None:
    """Bumps review_jobs.last_heartbeat_at on a fixed interval while the job is running.
    Interval comes from core/config.Settings.heartbeat_interval_seconds
    (env var YAAOF_HEARTBEAT_INTERVAL_SECONDS; default 10 in prod, 1 in tests).
    Used by the UI to detect 'stuck' jobs (no heartbeat in N minutes ⇒ stuck)
    and by the admin Activity page to show progress."""
    interval = get_settings().heartbeat_interval_seconds
    try:
        while True:
            await sql("UPDATE review_jobs SET last_heartbeat_at=now() WHERE id=:j", j=job_id)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        return
```

The handler can also write `current_step` (free-form string like `"assembling prompt"`, `"invoking agent"`, `"posting review"`) at coarse phases for richer UI progress. Phase transitions are reflected in `current_step` only — there is no `review_job.step_changed` audit entry per transition.

## Denormalized fields on `review_jobs`

Beyond the lifecycle columns (`status`, `started_at`, `completed_at`, `last_heartbeat_at`, `current_step`), the row also carries denormalized fields the UI reads frequently:

- `prompt_hash` — written at prompt-assembly time (step 8 of the handler), identical to the hash in the `review_job.prompt_sent` audit payload.
- `lessons_applied: list[UUID]` — IDs of the lessons that went into the prompt. UI uses this to render lesson chips next to findings (via `Finding.applied_lesson_ids`) and to link from the agent card to `/memory`. Per-lesson aggregate counts are **not** maintained (no `applied_count` on lessons).
- `tokens_in`, `tokens_out`, `cost_usd` — copied from `ReviewResult.telemetry` on success so the UI doesn't have to dig through audit payloads to display them on the agent card.
- `duration_s` — computed as `completed_at - started_at` and persisted on completion so list views can sort/aggregate without recomputing.

These are read-only denormalizations of data also captured in the audit log. The audit log remains the historical truth; the row is the convenience view.

## Cancellation handling inside the handler

The handler bails out at three points:

1. **Before flipping to `running`** (just after waking from debounce).
2. **Inside the handler before the workspace is provisioned** — checked at the start, but a long-running step (lessons fetch, diff fetch) could span time during which the job was cancelled.
3. **After workspace is provisioned, before calling coding_agent.review** — last chance to bail before the expensive subprocess.

If status is no longer `running` at any check, the handler returns early. The workspace context manager closes the workspace (marked `expired`; reaper destroys). No GitHub post happens.

## Prompt assembly and verdict computation

**Not in reviewer.** Both moved to `plugins/claude_code` as of 2026-05-16 — see [plugins-claude_code.md](plugins-claude_code.md). Reviewer assembles a `ReviewContext` (PR, diff, lessons, persona, prior comments) and hands it to `coding_agent.review`; the plugin owns the system framing, the output JSON schema, and the verdict logic (APPROVED / CHANGES_REQUESTED / COMMENT based on findings' severities).

## Reply workflow

`_run_reply_job(input: ReviewJobInput)`:

- Same overall shape as `_run_review_job` but with:
  - `kind='reply'` on the review_job row
  - Assembles a `ReplyContext` (PR, diff, persona, the human's reply body, the parent comment id) and calls `coding_agent.reply(plugin_id, ws, ctx) -> ReplyResult`. The plugin owns the prompt + the JSON output shape internally.
  - Output is posted as a reply to the parent comment via `vcs.post_comment_reply(pr, parent_id, result.body)`.
  - Not added to `posted_comments` (since it's a thread reply, not a top-level review).

## Startup recovery

On bootstrap, before the reaper / event loop is fully up:

```python
async def _startup_recovery():
    # Mark all 'running' jobs as 'failed' with crashed reason
    crashed = await sql("""
        UPDATE review_jobs
        SET status='failed', skip_reason='crashed',
            error_message='process crashed mid-execution',
            completed_at=now()
        WHERE status='running'
        RETURNING id
    """)
    for job_id in crashed:
        await audit_log.audit_for_review_job(
            job_id, kind='review_job.failed',
            payload=ReviewFailedPayload(
                invocation_status="crashed",
                error="yaaof restarted during execution",
                raw_output_excerpt="",
            ),
            actor=Actor(kind='system'),
            org_id=org_id,
        )

    # Queued jobs: respawn the handler coro (no stale task IDs, no broker — just
    # a fresh asyncio.create_task per row, with delay 0 since debounce-time
    # accountancy is best-effort across restarts in M01).
    queued = await sql("SELECT id, ticket_id, agent_id, org_id FROM review_jobs WHERE status='queued'")
    for row in queued:
        core.primitives.spawn(
            name=f"review_job:{row.id}",
            coro=_run_review_job(ReviewJobInput(
                review_job_id=row.id, ticket_id=row.ticket_id,
                agent_id=row.agent_id, org_id=row.org_id, debounce_seconds=0,
            )),
        )
```

Crashed `running` jobs are marked failed; operator re-triggers via the UI re-review button if needed (or the next push will auto-re-review). Queued jobs auto-resume because they had no side effects yet.

`_startup_recovery` is registered via the module's `RouteSpec`:

```python
# domain/reviewer/__init__.py
register_routes(RouteSpec(
    module_name="reviewer",
    router=router,
    on_startup=[_startup_recovery],
))
```

`core/webserver`'s lifespan runs every spec's `on_startup` hooks after routes are mounted (see [webserver.md](webserver.md#lifespan-implementation)). A hook that raises crashes the boot — startup failures are loud, not silent.

## Audit log entries

| Kind | When | Payload |
|---|---|---|
| `review_job.scheduled` | `schedule_review` / `schedule_reply` creates a new row | `{trigger_reason, agent_id, debounce_seconds}` |
| `review_job.cancelled` | Job superseded by a newer schedule or explicitly cancelled | `{reason: 'superseded' / 'ticket_closed' / 'repo_removed'}` |
| `review_job.prompt_sent` | Frozen-snapshot recorded just before agent invocation (also marks "running" in the timeline — no separate `started` entry) | `{agent_dump, prompt_hash, lessons_count, checkout_sha}` |
| `review_job.posted` | Successful post to GitHub | `{verdict, finding_count, tokens_in, tokens_out, cost_usd, latency_ms, review_external_id}` |
| `review_job.failed` | AgentInvocationResult was non-success | `{invocation_status, error, raw_output_excerpt}` |
| `review_job.skipped` | Pre-check rejected the diff | `{skip_reason}` |
| `review_job.reply_posted` | Successful reply to a comment thread | `{comment_external_id, parent_comment_external_id, tokens, cost, latency}` |

All written via `audit_log.audit_for_review_job(...)`. Entity is the ReviewJob; the audit timeline shown in the ticket detail page also queries audit entries for the ticket/PR/repo to assemble the full picture.

## Errors

`reviewer`'s handler is a fire-and-forget background coro spawned via `core/primitives.spawn` — it catches any non-`AgentInvocationResult` exception and converts it to a `failed` row. The `spawn()` wrapper attaches a structured log + OTel span around the entire coro, so failures are still observable even though nothing is "tracking" the coro from outside. The `result.status` enum handles known agent-side failures; everything else is infrastructure.

## What `domain/reviewer` does NOT do

- Does not own lessons — `domain/memory` does.
- Does not call LLMs directly. Period.
- Does not detect language at the file level — that's a helper in `repos` (sampled once on first review).
- Does not implement secret detection from scratch — uses a helper (regex + entropy) that lives... actually, where does secret detection live? Probably a small utility in `reviewer` itself for M01. If it grows, extract.
- Does not post arbitrary comments — only via `vcs.post_review` (and the new `post_comment_reply` for replies). No raw GitHub API calls.
- Does not handle force-push outdating — that's `vcs.mark_comments_outdated` called by `intake`.

## Decisions

### 2026-05-15 — Three public functions on the reviewer interface
`schedule_review`, `schedule_reply`, `cancel_pending`. Explicit; grep-able from callers.

### 2026-05-15 — Crashed `running` jobs marked `failed` on startup; not auto-rescheduled
Operator re-triggers via the UI re-review button if needed. Avoids infinite-loop on systematic crash causes.

### 2026-05-15 — Reply is a separate workflow (`_run_reply_job`), not a full review
Different shape from review (single body string, not a list of findings), posted as a thread reply rather than a top-level review. Reuses the workspace + coding_agent infrastructure via the Protocol's `reply` method.

### 2026-05-16 — Reviewer is a coding_agent consumer, not a prompt assembler
Previously `domain/reviewer/prompt.py` assembled the LLM prompt and `finding_types.py` defined the response schema; reviewer then called `core.coding_agent.invoke(prompt, response_model=FindingList)` and converted DTOs to `vcs.Finding`. As of 2026-05-16, prompt assembly + schema choice + DTO conversion all live in `plugins/claude_code`, and reviewer just builds a `ReviewContext` / `ReplyContext` and calls `coding_agent.review` / `coding_agent.reply`. `coding_agent` moved from `core/` to `domain/` (it returns `vcs.Finding`).
**Why:** see [coding_agent.md § Decisions](coding_agent.md#decisions). The reviewer's job is to drive the review *workflow* (scheduling, debouncing, cancellation, audit, post-to-VCS). Knowing how to talk to a specific agent CLI is the plugin's job. Splitting them makes the reviewer code smaller and makes future M02+ consumers of `coding_agent` (implementer, TODO-resolver) trivial to add without re-inventing prompt conventions.

### 2026-05-15 — No cross-agent visibility within a single batch
Three agents run in parallel and don't see each other's comments from the current batch. Each sees comments from PRIOR batches via `vcs.list_yaaof_comments`. Simpler; no synchronization.

### 2026-05-15 — Frozen snapshot is the agent dump + prompt hash + lesson count + checkout sha
Captured in `audit_entries(kind='review_job.prompt_sent')`. Full prompt text is NOT stored (could be 100KB+ per review); a SHA256 hash is stored so we can correlate to past runs. Raw prompt is captured in `raw_output` only on PARSE_FAILURE / AGENT_ERROR for debugging. Lesson IDs / content are NOT captured — users assume current lessons reflect what was applied.

### 2026-05-15 — `posted_comments` rows written by reviewer on every successful `post_review`
One row per finding-that-became-a-comment. Used by `intake` for reply-agent lookup. Reviewer's responsibility (it's the writer).

### 2026-05-15 — In-flight tracking lives in `review_jobs`; no generic task layer
`review_jobs` carries `started_at`, `last_heartbeat_at`, and `current_step`. A heartbeat coro bumps `last_heartbeat_at` every 10s while running. `list_in_flight()` returns `(queued, running)` rows; the admin/ops view reads from there. Cancellation is DB state flip + cooperative polling at safe points. Crashed-on-restart `running` rows are marked `failed`; `queued` rows are respawned.
**Why:** the thing being tracked is a domain entity (a review attempt) with rich state, not an opaque task. A generic queue would force the domain to layer this state on top anyway.

### 2026-05-15 — Reviewer owns the `reviewer_agents` table
Agents are review-specific. CRUD + seeding + HTTP routes for reviewer agents live in this module. M02+ `domain/implementer` will own its own `implementer_agents` table.
**Why:** DDD aggregate cohesion — a workflow and its agents are tightly coupled. Cross-workflow agent sharing is YAGNI; if it ever becomes a real need, the generic concept gets extracted then.
