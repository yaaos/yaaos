# domain/reviewer

> Review workflow orchestrator + durable findings. Two generations live here: today's `ReviewJob` per-PR queue and the new `PRReviewAggregate` with first-class `Finding` + state machine + acknowledgments + threads.

## Purpose

Owns every artifact tied to "what yaaos has said about a PR". That covers the per-run lifecycle (queue, debounce, secrets pre-flight, frozen-snapshot audit, step-progress SSE, startup recovery — generation 1) AND the durable findings layer that survives across reruns (multi-review history, fingerprint matching, persistent acknowledgments, comment threads, classified developer replies, verify-fix + stale-check evidence — generation 2). Does not call LLMs for code review itself — `domain/coding_agent` plugins do; only the reply classifier here makes a direct LLM call (via `core/llm`).

Two generations coexist while plan/notes/full-pr-flow.md §13 step 7's cut-over is in flight:

- Generation 1 — `ReviewJob` row, JSONB findings on the row, `schedule_review` → `coding_agent.review` → `vcs.post_review`. Today's public surface.
- Generation 2 — `PRReviewAggregate` per PR, owning `Review`s, `Finding`s, `FindingObservation`s, `CommentThread`s, `CommentMessage`s, `AcknowledgmentDecision`s. Read API (`findings/by-ticket`, `conversations/by-ticket`) is live; the auto-incremental + reply + verify-fix + stale-check write paths land alongside the UI surface.

## Public interface

Exported from `app/domain/reviewer/__init__.py`:

- Generation-1 types — `ReviewJob`, `ReviewJobInput`, `ReviewJobStatusChanged`, `ReviewJobRow`, `PostedCommentRow`.
- Generation-1 functions — `schedule_review`, `cancel_pending`, `get_review_job`, `list_review_jobs_for_pr`, `list_in_flight`, `metrics_summary`, `startup_recovery`.
- Generation-2 aggregate — `PRReviewAggregate`, `RawFinding`, `AdmissionDrop`.
- Generation-2 value objects — `Finding`, `FindingState`, `FindingFingerprint`, `CodeAnchor`, `CommentThread`, `CommentMessage`, `AcknowledgmentDecision`, `Review`, `ReviewScope`, `ReviewScopeKind`, `ReviewTrigger`, `Severity`, `AckKind`, `AuthorKind`, `ReplyIntent`.
- Generation-2 storage — `FindingRow`, `FindingObservationRow`, `CommentThreadRow`, `CommentMessageRow`, `AcknowledgmentDecisionRow`, `AggregateRepository` Protocol, `SqlAlchemyAggregateRepository`.
- Generation-2 concurrency — `acquire_pr_lock(session, pr_id)`.
- Generation-2 trigger policy — `decide_trigger`, `TriggerInputs`, `TriggerDecision` (`Skip` | `Debounce` | `Run`), `humanize_skip`.
- Generation-2 reply classifier — `ClassifyReplyInput`, `ClassifyReplyOutput`, `classify_reply`, `classify_reply_runnable`.
- Generation-2 service helpers — `apply_classified_reply`, `apply_verify_fix_result`, `apply_stale_check_result`, `is_yaaos_command`, `is_off_topic_message`, `list_findings_view`, `all_conversations_view`, `review_summary`, plus `ReplyAction` / `VerifyFixAction` / `StaleCheckAction` / `FindingView` / `ConversationView`, and the `CLASSIFY_*` / `VERIFY_*` thresholds.
- Generation-2 events — `ReviewRequested`, `ReviewStarted`, `ReviewCompleted`, `ReviewFailed`, `ReviewSuperseded`, `FindingRaised`, `FindingReObserved`, `FindingStateChanged`, `FindingAcknowledged`, `FindingResolutionDetected`, `FindingStaleDetected`, `FindingAnchorUpdated`, `CommentReplyReceived`, `AgentReplyPosted`, plus `DomainEvent` union.

HTTP routes (`/api/reviewer`):

- `POST /rereview` — body `{ ticket_id }`; UI button.
- `POST /cancel?ticket_id=…` — cancel queued/running job.
- `GET /jobs/by-ticket/{ticket_id}` — every review_job for the ticket's PR (generation 1).
- `GET /findings/by-ticket/{ticket_id}?include_terminal=…` — list open + acknowledged findings (generation 2). Set the query param to `true` to also return resolved + stale.
- `GET /conversations/by-ticket/{ticket_id}` — All-Conversations cross-cut (generation 2).
- `GET /metrics` — aggregate counters.

`RouteSpec` registers one `on_startup` hook: `startup_recovery`.

## Module architecture

### Entities

- `ReviewJob` — generation-1 run-level state per `(PR x run)`: status, heartbeat, model, effort, JSONB findings, JSONB activity log. Identity = `id` UUID.
- `Review` — generation-2 run-level state with per-PR `sequence_number`, `trigger_reason`, `scope`, `commit_sha_at_start`, `superseded_by_review_id`, `pending_replay`. Lives in-aggregate today; persists to `review_jobs` via the §13 step 7 cut-over.
- `Finding` — durable per-PR finding identified across reviews via `FindingFingerprint`. Sticky severity, max-over-observations confidence, anchor that drifts under code changes. State machine: `open → acknowledged | resolved_confirmed | resolved_unverified | stale`.
- `FindingObservation` — append-only `(finding × review)` sighting; carries the anchor at the time + the agent's raw body.
- `CommentThread` — 1:1 with `Finding`. Carries the GitHub-side `external_thread_id` indexed for webhook resolution.
- `CommentMessage` — every yaaos- and human-authored message; carries `external_comment_id` indexed plus optional `classified_intent` + `classification_confidence` on humans.
- `AcknowledgmentDecision` — persistent dev intent to skip a finding (`intentional` | `wontfix`); survives every future review.

### Key value objects

- `FindingFingerprint` — conceptual identity across reviews: `(file_path, rule_id, anchor_content_hash, body_gist_hash)`. Whitespace-normalized hashes so reindents don't churn fingerprints (plan §2.3).
- `CodeAnchor` — `(file_path, line_start, line_end, surrounding_content_hash, commit_sha)`. The surrounding hash covers ±3 lines and is what lets `anchor.resolve_anchor` re-find the position after line drift.
- `FindingState`, `Severity`, `AckKind`, `ReplyIntent`, `AuthorKind`, `ReviewTrigger`, `ReviewScope` — enums + frozen dataclasses per plan §2.3.
- `RawFinding` — coding-agent output before admission; must include `concrete_failure_scenario` (plan §10.1) or the aggregate drops it.
- `AdmissionDrop` — audit-log payload for a rejected raw finding: `(rule_id, reason, severity, confidence)` where reason ∈ `malformed | below_threshold | nit_cap | top_cap | matches_ack`.

### Core user flows

#### Generation 1 — `schedule_review` (today)

1. `intake.schedule_review` for `pr_ready` / `pr_synchronized` / `rereview_command` / UI button cancels any in-flight job for the PR, inserts a queued `ReviewJobRow`, writes `review_job.scheduled` audit, publishes `ReviewJobStatusChanged(queued)`, spawns `_run_review_job`.
2. Worker debounces, flips to `running`, resolves entities + diff, runs `_ticket_skip_reason` (`fork` / `bot_author` / `trivial_diff` / `too_large`), runs secrets pre-flight, language-detects, provisions `in_process` workspace (head + base SHAs, branch names).
3. Builds `ReviewContext` from PR + diff + lessons + prior yaaos comments. Hashes the context; writes `review_job.prompt_sent` audit; calls `coding_agent.review(plugin_id="claude_code", ws, ctx)`.
4. Builds `vcs.Review(agent_tag="yaaos", state, summary_body, findings)`; the github plugin posts each finding as its own comment (inline vs top-level by anchor presence) with a per-agent emoji suffix.
5. Persists `PostedCommentRow` per finding-as-comment, updates the row to `posted` with telemetry + JSONB findings, writes `review_job.posted` audit, publishes `ReviewJobStatusChanged(posted)`.

#### Generation 2 — durable findings

1. **Initial review on PR ready** (plan §6.1, write path lands with §13 step 7's cut-over): service acquires the per-PR advisory lock, opens a transaction, loads the aggregate, starts a `Review` via `start_review`, invokes coding_agent in `full_review` mode, maps each `FindingDraft` → `RawFinding`, calls `aggregate.post_process_raw_findings` which applies the malformed / threshold / per-PR nit cap / cross-file dedup / per-review top-10 cap pipeline + dedup vs prior open/acknowledged findings. For each survivor: opens a thread, appends a yaaos message, posts via `vcs.post_review`. Completes the review; saves the aggregate; drains domain events.
2. **Auto-incremental on push** (plan §6.2, write path WIP): intake hands `(pr_id, new_head, prev_head)` to a trigger-policy helper. `trigger.decide_trigger` returns `Skip | Debounce | Run` per §7. On `Run`, the service schedules a debounced incremental review whose worker (a) invokes `coding_agent.incremental_review` on `prev_sha..head` and (b) for each open finding whose anchor file is in the diff, re-resolves the anchor; gone-without-verify → `mark_unverified_resolution`, anchor-moved → invoke `coding_agent.verify_fix` and route the result through `apply_verify_fix_result`. Re-observations dedup silently; new findings flow through the same admission pipeline.
3. **Developer reply** (plan §6.4): intake routes a `pull_request_review_comment` / `issue_comment` payload through a thread-resolution layer that finds the `CommentThread` by `external_thread_id`. The service runs the deterministic checks first — `is_yaaos_command` routes to the command handler, `is_off_topic_message` stores without classifying. Otherwise `classify_reply` runs through `core/llm`; `apply_classified_reply` decides what to do: ≥ 0.85 acknowledgment transitions and posts "Noted…", 0.60-0.84 posts the mid-band confirmation reply, ≥ 0.85 verify_fix kicks off the subflow, everything else stores the message silently.
4. **Verify-fix subflow** (plan §6.5): coding_agent runs in `verify_fix` mode with the original anchor's code + current code at the resolved anchor; the result feeds `apply_verify_fix_result` per plan §10.4 thresholds.
5. **Stale-check** (plan §6.2 step 4b): same shape as verify-fix but for anchor-moved-but-still-applies-maybe cases; `apply_stale_check_result` routes the outcome to `record_stale_detection` or a no-op observe.

### State machines

`FindingState` (per finding, per PR) — plan §3:

- `open`
- `acknowledged` (terminal in this PR for POC; `acknowledged → open` is deferred per plan §11)
- `resolved_confirmed` (terminal)
- `resolved_unverified` (terminal)
- `stale` (terminal)

Transitions:

- `(new) → open` — new fingerprint observed in a review.
- `open → acknowledged` — developer reply classified as `acknowledgment` with confidence ≥ `CLASSIFY_ACT_THRESHOLD` (0.85); carries an `AckKind`.
- `open → resolved_confirmed` — `verify_fix` returns "not present" with confidence ≥ `VERIFY_ACT_THRESHOLD` (0.80).
- `open → resolved_unverified` — anchor gone in the new commit and no verify-fix possible.
- `open → stale` — `stale_check` returns "no longer applies" with confidence ≥ `VERIFY_ACT_THRESHOLD`.

Pure transition functions in `state_machine.py`; the aggregate is the only legitimate caller. Low-confidence agent output never causes a state change — fallback is always to leave `open`.

### Per-PR queue discipline (generation 1)

"At most one in-flight `ReviewJob` per PR" — enforced by service logic, not a unique index. `schedule_review` flips every `queued`/`running` row for the PR to `cancelled` with `skip_reason='superseded'`, writes `review_job.cancelled` audit, inserts the new `queued` row, spawns the handler. Generation-2 concurrency is the PG advisory lock instead — `acquire_pr_lock(session, pr_id)` at the start of every mutating transaction.

### Concurrency (generation 2)

`lock.acquire_pr_lock` issues `pg_advisory_xact_lock(hashtext('pr:<uuid>')::bigint)` inside the calling transaction. Two webhook events for the same PR serialize cleanly; the lock releases automatically at commit/rollback. Read-only entry points (`list_*` / `get_*`) do NOT take the lock.

### Admission pipeline (plan §10)

Inside `aggregate.post_process_raw_findings`, in order:

1. **Schema gate** — drop raw findings missing `concrete_failure_scenario`. Audit reason: `malformed`.
2. **Per-severity threshold** — `blocker`/`major` ≥ 75, `minor` ≥ 85, `nit` ≥ 90 (plan §10.2). Audit reason: `below_threshold`.
3. **Per-PR nit cap** — at most 5 nits ever for this PR (plan §10.5). Audit reason: `nit_cap`.
4. **Fingerprint match** — vs prior findings on this PR: matches against `acknowledged` drop silently (`matches_ack`); matches against `open` re-observe with sticky severity + `max(stored, new)` confidence.
5. **Cross-file dedup** — collapse same-rule findings on multiple files into one comment with a file list (plan §10.8).
6. **Per-review top-10 cap** — rank by `severity_weight × confidence` (blocker=4, major=3, minor=2, nit=1); admit top 10. Re-observations don't count. Audit reason: `top_cap`.

### Cancellation — DB flip + task cancel (generation 1)

Two-track:

1. **DB-driven** — `cancel_pending` flips the row to `cancelled` and writes the `review_job.cancelled` audit. Always happens; what the UI reads.
2. **Task-driven** — `cancel_pending` also calls `asyncio.Task.cancel()` on the in-flight task (looked up in a module-level `_inflight_tasks` registry keyed by `review_job_id`). The cancellation propagates through `coding_agent.review` → `workspace.run_coding_agent_cli`, which catches `CancelledError`, kills the subprocess group (SIGTERM → 2s → SIGKILL), drains the pipes, and re-raises.

### Step-progress SSE (generation 1)

`_set_step` writes `current_step` + `last_heartbeat_at` and publishes `ReviewJobStepProgress`. Phases: `resolving_entities` → `fetching_diff` → `provisioning_workspace` → `invoking_agent` → `posting_review` → (`posted` | `failed`). Step changes generate no audit entries.

### Reviewer voice + noise control

Generation-2 prompts and prompt-side rules live in `domain/reviewer/llm/` (per plan §5.5):

- `prompts/classify_reply.prompt.md` — versioned `.prompt.md` file consumed by `core/llm`. Frontmatter pins the cheapest current Anthropic Haiku.
- Confidence rubric in the prompt's system block matches plan §10.3.

The "do NOT flag" list (plan §10.6) and the target-repo-conventions injection (plan §10.11) land alongside the coding-agent prompt-ownership migration; the contracts are already in place via `coding_agent.FindingDraft`.

## Data owned

Generation 1:

- `review_jobs` — one row per `(PR x run)`. Indexed on `(pr_id, status, created_at)` and `(status, last_heartbeat_at)`. Carries: status enum, heartbeat, current_step, prompt_hash, lessons_applied, tokens/duration, JSONB findings list, JSONB activity_log, model + effort.
- `posted_comments` — one row per VCS comment yaaos has posted; PK `external_comment_id`. Read by `intake` to resolve "which review_job owns this comment".

Generation 2 (plan §4.1):

- `findings` — first-class finding. UNIQUE `(pr_id, fingerprint_hash)`. Indexed on `(pr_id, state)` and `fingerprint_hash`. Carries: state, sticky severity, max-confidence, anchor JSONB, `concrete_failure_scenario`, `source_agent`, `first_seen_review_id` + `last_observed_review_id` (unconstrained UUID until §13 step 7 renames `review_jobs` → `reviews`).
- `finding_observations` — append-only `(finding, review)` sightings. Indexed on `(finding_id, review_id)`. Each row carries the anchor at the time and the agent's raw body.
- `comment_threads` — 1:1 with `findings`. `external_thread_id` indexed for webhook resolution.
- `comment_messages` — every yaaos- and human-authored message. `external_comment_id` indexed. `classified_intent` + `classification_confidence` populated for human messages by `classify_reply`.
- `acknowledgment_decisions` — persistent dev decisions. Survive future reviews — re-observed fingerprints with an ack drop silently in the admission pipeline.

`review_id` columns on generation-2 tables are unconstrained UUIDs by design; the `review_jobs → reviews` rename in §13 step 7 turns them into a real FK. Canonical schema in [core_database.md](core_database.md).

## How it's tested

- **Unit tests** for `state_machine.py`, `fingerprint.py`, `anchor.py`, `trigger.py`, the aggregate, the service helpers (`apply_classified_reply` / `apply_verify_fix_result` / `apply_stale_check_result` / `is_yaaos_command` / `is_off_topic_message`), and the classifier (with a canned-output runnable substituting `core/llm`).
- **In-memory `AggregateRepository`** at `test/in_memory_repository.py` exercises full scenarios from plan §6 — admission pipeline (threshold, nit cap, top cap, cross-file dedup, fingerprint match vs prior open/acknowledged), state transitions, round-trip persistence.
- **Integration coverage** for generation 1 (`test_detect_secrets.py`, plus the scheduling / supersession / handler / startup-recovery suites in `app/test/` + `apps/e2e/`) continues to gate today's flow.
- **E2E** for the durable-findings flow (multi-review render + single reply round-trip) lands with the UI commits.
- **Evals** for the `classify_reply` prompt live under `domain/reviewer/eval/` (one `.eval.py` per prompt + fixtures); evals deliberately bypass `langchain.cache.SQLiteCache` so they always hit the model fresh.
