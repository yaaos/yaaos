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
- Generation-2 reply classifier — `ClassifyReplyInput`, `ClassifyReplyOutput`, `classify_reply`. Single-shot call into `core/llm`'s `PromptRunnable`; no DI hook on the function — tests rely on the file-colocated `LLMTestCache` for deterministic replay.
- Generation-2 service helpers — `apply_classified_reply`, `apply_verify_fix_result`, `apply_stale_check_result`, `is_yaaos_command`, `is_off_topic_message`, `list_findings_view`, `all_conversations_view`, `review_summary`, plus `ReplyAction` / `VerifyFixAction` / `StaleCheckAction` / `FindingView` / `ConversationView`, and the `VERIFY_*` thresholds. Reply classification is purely categorical — no confidence thresholds.
- Generation-2 plan §5.1 public Python API — `list_reviews_for_pr(pr_id, *, org_id)`, `get_review(review_id, *, org_id)`, `list_findings_for_pr(pr_id, *, org_id, include_terminal=False)`, `get_thread(thread_id, *, org_id)`. View dataclasses `ThreadView`, `ThreadMessageView`.
- Generation-2 plan §10.13 metrics — `compute_acceptance_rate(*, org_id)` (`resolved_confirmed + resolved_unverified` / total), `compute_resolved_without_edit_rate(*, org_id)` (`acknowledged + resolved_unverified + stale` / total).
- Generation-2 save-time hooks — `dispatch_events(aggregate)` drains the aggregate's pending events to the `core/events` bus via a `_DomainEventEnvelope` adapter; `dispatch_audits(aggregate, *, session, actor, org_id)` writes one `audit_entries` row per finding state-changing event (`FindingRaised`, `FindingReObserved`, `FindingAnchorUpdated`, `FindingStateChanged`, `FindingAcknowledged`, `FindingResolutionDetected`, `FindingStaleDetected`). Service-layer callers invoke both after every `agg_repo.save(aggregate)`.
- Generation-2 events — `ReviewRequested`, `ReviewStarted`, `ReviewCompleted`, `ReviewFailed`, `ReviewSuperseded`, `FindingRaised`, `FindingReObserved`, `FindingStateChanged`, `FindingAcknowledged`, `FindingResolutionDetected`, `FindingStaleDetected`, `FindingAnchorUpdated`, `CommentReplyReceived`, `AgentReplyPosted`, plus `DomainEvent` union.

HTTP routes (`/api/reviewer`):

- `POST /rereview` — body `{ ticket_id }`; UI button.
- `POST /cancel?ticket_id=…` — cancel queued/running job.
- `GET /jobs/by-ticket/{ticket_id}` — every review_job for the ticket's PR (generation 1).
- `GET /findings/by-ticket/{ticket_id}?include_terminal=…` — list open + acknowledged findings (generation 2). Set the query param to `true` to also return resolved + stale.
- `GET /conversations/by-ticket/{ticket_id}` — All-Conversations cross-cut: findings whose thread has ≥1 developer (`author_kind='human'`) message. Terminal-state findings (resolved_*, stale) are excluded. Findings yaaos raised but the developer never replied to don't appear here — the per-review timeline already surfaces those.
- `GET /metrics` — aggregate counters.

`RouteSpec` registers one `on_startup` hook: `startup_recovery`.

## Module architecture

### Entities

- `ReviewJob` — generation-1 run-level state per `(PR x run)`: status, heartbeat, model, effort, JSONB findings, JSONB activity log. Identity = `id` UUID.
- `Review` — generation-2 run-level state with per-PR `sequence_number`, `trigger_reason`, `scope`, `commit_sha_at_start`, `superseded_by_review_id`, `pending_replay`. Lives in-aggregate today; persists to `review_jobs` via the §13 step 7 cut-over.
- `Finding` — durable per-PR finding identified across reviews via `FindingFingerprint`. Sticky severity, max-over-observations confidence, anchor that drifts under code changes. State machine: `open → acknowledged | resolved_confirmed | resolved_unverified | stale`.
- `FindingObservation` — append-only `(finding × review)` sighting; carries the anchor at the time + the agent's raw body.
- `CommentThread` — 1:1 with `Finding`. Carries the GitHub-side `external_thread_id` indexed for webhook resolution.
- `CommentMessage` — every yaaos- and human-authored message; carries `external_comment_id` indexed plus optional `classified_intent` on humans. The intent label encodes the routing decision — no separate confidence field.
- `AcknowledgmentDecision` — persistent dev intent to skip a finding (`intentional` | `wontfix`); survives every future review.

### Key value objects

- `FindingFingerprint` — conceptual identity across reviews: `(file_path, rule_id, anchor_content_hash, body_gist_hash)`. Whitespace-normalized hashes so reindents don't churn fingerprints (plan §2.3).
- `CodeAnchor` — `(file_path, line_start, line_end, surrounding_content_hash, commit_sha)`. The surrounding hash covers ±3 lines and is what lets `anchor.resolve_anchor` re-find the position after line drift.
- `FindingState`, `Severity`, `AckKind`, `ReplyIntent`, `AuthorKind`, `ReviewTrigger`, `ReviewScope` — enums + frozen dataclasses per plan §2.3.
- `RawFinding` — coding-agent output before admission; must include `concrete_failure_scenario` ≥ 20 chars (plan §10.1) or the aggregate drops it. Built from `coding_agent.FindingDraft` via `_findingdrafts_to_raw` in `queue.py`; admitted survivors are translated back to `vcs.Finding` for posting via `_raw_to_vcs_findings`. These two are the only shared converters; the legacy `_vcs_findings_to_raw` is gone.
- `AdmissionDrop` — audit-log payload for a rejected raw finding: `(rule_id, reason, severity, confidence)` where reason ∈ `malformed | below_threshold | nit_cap | top_cap | matches_ack`.

### Core user flows

#### Generation 1 — `schedule_review` (today)

1. `intake.schedule_review` for `pr_ready` / `pr_synchronized` / `rereview_command` / UI button cancels any in-flight job for the PR, inserts a queued `ReviewJobRow`, writes `review_job.scheduled` audit, publishes `ReviewJobStatusChanged(queued)`, spawns `_run_review_job`.
2. Worker debounces, flips to `running`, resolves entities + diff, runs `_ticket_skip_reason` (`fork` / `bot_author` / `trivial_diff` / `too_large`), runs secrets pre-flight, language-detects, builds the per-review MCP payload via `_build_mcp_payload` (walks `domain/integrations.known_providers()`, includes only `enabled=True` rows whose `last_refresh_status != "failed"`; mints a `mcp_review_tokens` bearer when at least one provider survives), provisions `in_process` workspace (head + base SHAs, branch names).
3. Builds `ReviewContext` from PR + diff + lessons + the MCP payload on `agent_config["mcp"]`. `prior_yaaos_comment_bodies` is populated on the context but NOT surfaced into the prompt — the aggregate's fingerprint dedup (§10.10) handles re-observation silently; instructing the agent to avoid duplicates would starve the re-observation signal. Hashes the context; writes `review_job.prompt_sent` audit; calls `coding_agent.review(plugin_id="claude_code", ws, ctx)`. The plugin materializes `.mcp.json` inside the workspace from `agent_config["mcp"]` so the CLI can call `mcp__<server>__<tool>`. Token is revoked via `domain/mcp_proxy.revoke_token(review_id)` before the workspace context exits — read failures still revoke. The agent returns `list[FindingDraft]` (§10.1 schema).
4. Acquires the per-PR advisory lock, loads the aggregate, converts drafts via `_findingdrafts_to_raw`, runs `aggregate.post_process_raw_findings(..., diff_files=...)`, then translates admitted survivors back to `vcs.Finding` via `_raw_to_vcs_findings` and posts via `vcs.post_review`. Rejected drafts never reach GitHub. The github plugin posts each survivor as its own comment (inline vs top-level by anchor presence) with a per-agent emoji suffix.
5. Persists `PostedCommentRow` per finding-as-comment, updates the row to `posted` with telemetry + JSONB findings (admitted-only entries: `{file_path, line_start, line_end, severity (§10.1 enum), rule_id, title, body, rationale, source_agent}`), writes `review_job.posted` audit, publishes `ReviewJobStatusChanged(posted)`. Calls `dispatch_audits` + `dispatch_events` after the aggregate save.

#### Generation 2 — durable findings

1. **Initial review on PR ready** (plan §6.1, write path lands with §13 step 7's cut-over): service acquires the per-PR advisory lock, opens a transaction, loads the aggregate, starts a `Review` via `start_review`, invokes coding_agent in `full_review` mode, maps each `FindingDraft` → `RawFinding`, calls `aggregate.post_process_raw_findings` which applies the malformed / threshold / per-PR nit cap / cross-file dedup / per-review top-10 cap pipeline + dedup vs prior open/acknowledged findings. For each survivor: opens a thread, appends a yaaos message, posts via `vcs.post_review`. Completes the review; saves the aggregate; drains domain events.
2. **Auto-incremental on push** (plan §6.2, write path WIP): intake hands `(pr_id, new_head, prev_head)` to a trigger-policy helper. `trigger.decide_trigger` returns `Skip | Debounce | Run` per §7. On `Run`, the service schedules a debounced incremental review whose worker (a) invokes `coding_agent.incremental_review` on `prev_sha..head` and (b) runs the deterministic anchor pass `resolve_open_anchors(aggregate, *, touched_files, read_file, new_commit_sha)` (in `incremental.py`) BEFORE any LLM stale_check. That pure helper returns a `ResolveAnchorsResult` partitioning open findings into `moved` / `gone` / `unchanged`; `gone` transitions to `resolved_unverified`, `moved` is fed into `coding_agent.verify_fix` and routed through `apply_verify_fix_result`, `unchanged` stays put. New findings flow through the same admission pipeline. Anchor mutations happen on a snapshot aggregate loaded inside the workspace block (no long-lived DB session); the moves are replayed onto a freshly-loaded live aggregate at save time.
3. **Developer reply** (plan §6.4): intake routes a `pull_request_review_comment` / `issue_comment` payload through a thread-resolution layer that finds the `CommentThread` by `external_thread_id`. The service runs the deterministic checks first — `is_yaaos_command` routes to the command handler, `is_off_topic_message` stores without classifying. Otherwise `classify_reply` runs through `core/llm` and emits exactly one of five categorical intents; `apply_classified_reply` maps that label 1:1 onto a `ReplyAction`:

   | intent                    | action                                              |
   | ------------------------- | --------------------------------------------------- |
   | `acknowledgment_clear`    | `acknowledge_posted` — state → acknowledged + reply |
   | `acknowledgment_unclear`  | `confirm_requested` — post mid-band confirm prompt  |
   | `verify_fix`              | `verify_fix_triggered` — spawn workspace subflow    |
   | `question`                | `answer_question_triggered` — spawn workspace subflow |
   | `other`                   | `noop` — store message, stay silent                 |

   No confidence axis — the LLM picks one label and the label encodes the action. Best-practice for LLM classification: categorical labels with examples per label outperform a float-probability output that the model can't actually calibrate. When the developer responds `confirm` to a mid-band prompt, `_original_mid_band_rationale(thread_id, author_external_id)` in `replies.py` walks the thread chronologically to find the last human message from the same author posted BEFORE the yaaos confirm-request, and uses its body (not the literal "confirm") as the persisted ack rationale.
4. **Verify-fix subflow** (plan §6.5): the `verify_fix` runner in `replies.py` provisions a workspace at HEAD, reads `current_anchor.original_lines` (captured at finding-creation by `make_anchor()` and carried forward by `resolve_anchor()`) as the original code, reads the current code at the resolved anchor via `workspace.read_text`, and hands both snippets to `coding_agent.verify_fix`. The result feeds `apply_verify_fix_result` per plan §10.4 thresholds. `original_lines` is persisted in the `current_anchor` JSONB blob — no migration needed.
5. **Answer-question subflow**: `_run_answer_question` in `replies.py` provisions a workspace at HEAD with read-only repo + git tool access (no `Task` subagent dispatch), passes the finding context + the full thread history + the developer's question to `coding_agent.answer_question`, and posts the agent's single-text answer as a yaaos reply on the thread. No state transition — questions don't acknowledge or resolve the finding, they just produce an inline explanation.
6. **Stale-check** (plan §6.2 step 4b): same shape as verify-fix but for anchor-moved-but-still-applies-maybe cases; `apply_stale_check_result` routes the outcome to `record_stale_detection` or a no-op observe.

### State machines

`FindingState` (per finding, per PR) — plan §3:

- `open`
- `acknowledged` (terminal in this PR for POC; `acknowledged → open` is deferred per plan §11)
- `resolved_confirmed` (terminal)
- `resolved_unverified` (terminal)
- `stale` (terminal)

Transitions:

- `(new) → open` — new fingerprint observed in a review.
- `open → acknowledged` — developer reply classified as `acknowledgment_clear`; carries an `AckKind`. (`acknowledgment_unclear` does NOT transition — it posts a confirm request and waits for the literal `confirm` reply.)
- `open → resolved_confirmed` — `verify_fix` returns "not present" with confidence ≥ `VERIFY_ACT_THRESHOLD` (0.80).
- `open → resolved_unverified` — anchor gone in the new commit and no verify-fix possible.
- `open → stale` — `stale_check` returns "no longer applies" with confidence ≥ `VERIFY_ACT_THRESHOLD`.

Pure transition functions in `state_machine.py`; the aggregate is the only legitimate caller. Low-confidence agent output never causes a state change — fallback is always to leave `open`.

### Per-PR queue discipline (generation 1)

"At most one in-flight `ReviewJob` per PR" — enforced by service logic, not a unique index. `schedule_review` flips every `queued`/`running` row for the PR to `cancelled` with `skip_reason='superseded'`, writes `review_job.cancelled` audit, inserts the new `queued` row, spawns the handler. Generation-2 concurrency is the PG advisory lock instead — `acquire_pr_lock(session, pr_id)` at the start of every mutating transaction.

### Concurrency (generation 2)

`lock.acquire_pr_lock` issues `pg_advisory_xact_lock(hashtext('pr:<uuid>')::bigint)` inside the calling transaction. Two webhook events for the same PR serialize cleanly; the lock releases automatically at commit/rollback. Read-only entry points (`list_*` / `get_*`) do NOT take the lock.

### Admission pipeline (plan §10)

Inside `aggregate.post_process_raw_findings(review_id, raw, *, diff_files=None)`, in order:

1. **Schema gate** — drop raw findings whose `concrete_failure_scenario` is missing or under `_MIN_SCENARIO_LEN` (20 chars stripped). Closes the legacy synthesis loophole where a one-word body would otherwise pass. Audit reason: `malformed`.
2. **Off-diff drop** — when `diff_files` is supplied (queue.py for full review, incremental.py for incremental), findings whose anchor file isn't in the PR diff are dropped (plan §10.9). Audit reason: `off_diff`.
3. **Per-severity threshold** — `blocker`/`major` ≥ 75, `minor` ≥ 85, `nit` ≥ 90 (plan §10.2). Audit reason: `below_threshold`.
4. **Per-PR nit cap** — at most 5 nits ever for this PR (plan §10.5). Audit reason: `nit_cap`.
5. **Fingerprint match** — vs prior findings on this PR: matches against `acknowledged` drop silently (`matches_ack`); matches against `open` re-observe with sticky severity + `max(stored, new)` confidence.
6. **Cross-file dedup** — same-rule findings on multiple files collapse into one survivor whose body gains an "Also in: file2, file3, …" footer enumerating the duplicated paths (plan §10.8).
7. **Per-review top-10 cap** — rank by `severity_weight × confidence` (blocker=4, major=3, minor=2, nit=1); admit top 10. Re-observations don't count. Audit reason: `top_cap`.

Admission runs BEFORE `vcs.post_review` in `queue.py`'s full-review flow — rejected drafts never reach GitHub.

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
- `comment_messages` — every yaaos- and human-authored message. `external_comment_id` indexed. `classified_intent` populated for human messages by `classify_reply`.
- `acknowledgment_decisions` — persistent dev decisions. Survive future reviews — re-observed fingerprints with an ack drop silently in the admission pipeline.

`SqlAlchemyAggregateRepository.save` flushes in FK order: findings → flush → observations + threads → flush → messages → flush → acks. It also persists `Review` row updates (status, `commit_sha_at_start`, `superseded_by_review_id`, `pending_replay`, `started_at` / `completed_at` timestamps) via `_review_from_row` — initial `ReviewRow` INSERT still lives in `queue.py` / `incremental.py` because those callers hold the per-PR advisory lock and assign `sequence_number`.

`review_id` columns on generation-2 tables are unconstrained UUIDs by design; the `review_jobs → reviews` rename in §13 step 7 turns them into a real FK. Canonical schema in [core_database.md](core_database.md).

## How it's tested

- **Unit tests** for `state_machine.py`, `fingerprint.py`, `anchor.py`, `trigger.py`, the aggregate, the service helpers (`apply_classified_reply` / `apply_verify_fix_result` / `apply_stale_check_result` / `is_yaaos_command` / `is_off_topic_message`), and the classifier (with a canned-output runnable substituting `core/llm`).
- **In-memory `AggregateRepository`** at `test/in_memory_repository.py` exercises full scenarios from plan §6 — admission pipeline (threshold, nit cap, top cap, cross-file dedup, fingerprint match vs prior open/acknowledged), state transitions, round-trip persistence.
- **Integration coverage** for generation 1 (`test_detect_secrets.py`, plus the scheduling / supersession / handler / startup-recovery suites in `app/test/` + `apps/e2e/`) continues to gate today's flow.
- **E2E** for the durable-findings flow (multi-review render + single reply round-trip) lands with the UI commits.
- **Evals** for the `classify_reply` prompt live under `domain/reviewer/eval/` (one `.eval.py` per prompt + fixtures); evals deliberately bypass `langchain.cache.SQLiteCache` so they always hit the model fresh.
