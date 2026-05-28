# domain/reviewer

> Review workflow orchestrator + durable findings. The workflow engine drives every review run; `PRReviewAggregate` owns `Finding` / state machine / acknowledgments / threads as the durable layer.

## Purpose

Owns every artifact tied to "what yaaos has said about a PR". The workflow engine (`core/workflow`) routes every review run — pr-ready, push-incremental, re-review, verify-fix, stale-check, answer-question — through one of five typed workflows whose `WorkflowCommand` bodies live under [`commands/`](../app/domain/reviewer/commands/). The durable layer is `PRReviewAggregate` per PR: `Review`s, `Finding`s, `FindingObservation`s, `CommentThread`s, `CommentMessage`s, `AcknowledgmentDecision`s. Does not call LLMs for code review itself — `domain/coding_agent` plugins do; only the reply classifier here makes a direct LLM call (via `core/llm`).

All call sites route through `start_pr_review` (full review) and `handle_push` (incremental), which fan out via the workflow engine; cancellation routes through `cancel_workflows_for_ticket` → `workflow.request_cancel`.

## Public interface

Exported from `app/domain/reviewer/__init__.py`:

- Entry points — `start_pr_review(ticket_id, *, org_id, trigger_reason)` starts a `pr_review_v1` workflow execution. `handle_push(pr_id, *, new_head_sha, prev_head_sha, org_id)` runs the trigger policy and spawns an `incremental.py` runner directly. `cancel_workflows_for_ticket(ticket_id)` flips every non-terminal `workflow_executions` row for the ticket via `workflow.request_cancel`.
- Read API — `ReviewJob` (projection shape over `workflow_executions`, see `workflow_review_view.py`).
- Generation-2 aggregate — `PRReviewAggregate`, `RawFinding`, `AdmissionDrop`.
- Generation-2 value objects — `Finding`, `FindingState`, `FindingFingerprint`, `CodeAnchor`, `CommentThread`, `CommentMessage`, `AcknowledgmentDecision`, `Review`, `ReviewScope`, `ReviewScopeKind`, `ReviewTrigger`, `Severity`, `AckKind`, `AuthorKind`, `ReplyIntent`.
- Generation-2 storage — `AggregateRepository` Protocol, `SqlAlchemyAggregateRepository`. Row classes (`FindingRow`, `CommentThreadRow`, etc.) are internal — import from the concrete submodule path `app.domain.reviewer.models` only when you must write SQLAlchemy queries against them; prefer the service ops below.
- Generation-2 concurrency — `acquire_pr_lock(session, pr_id)`.
- Generation-2 trigger policy — `decide_trigger`, `TriggerInputs`, `TriggerDecision` (`Skip` | `Debounce` | `Run`), `humanize_skip`.
- Generation-2 reply classifier — `ClassifyReplyInput`, `ClassifyReplyOutput`, `classify_reply`. Single-shot call into `core/llm`'s `PromptRunnable`; no DI hook on the function — tests rely on the file-colocated `LLMTestCache` for deterministic replay.
- Generation-2 service helpers — `apply_classified_reply`, `apply_verify_fix_result`, `apply_stale_check_result`, `is_yaaos_command`, `is_off_topic_message`, `list_findings_view`, `all_conversations_view`, `review_summary`, plus `ReplyAction` / `VerifyFixAction` / `StaleCheckAction` / `FindingView` / `ConversationView`, and the `VERIFY_*` thresholds. Reply classification is purely categorical — no confidence thresholds.
- Generation-2 plan §5.1 public Python API — `list_reviews_for_pr(pr_id, *, org_id)`, `get_review(review_id, *, org_id)`, `get_org_id_for_review(review_id) -> UUID | None` (unscoped fetch for callers that don't yet know the org), `list_findings_for_pr(pr_id, *, org_id, include_terminal=False)`, `get_thread(thread_id, *, org_id)`. View dataclasses `ThreadView`, `ThreadMessageView`.
- Cross-module aggregate query ops — `find_pr_id_by_external_comment_id(external_comment_id)` returns the `UUID` pr_id whose finding thread contains the given comment, or `None` when absent (used by intake's reaction handler); `aggregate_findings_by_prs(pr_ids, *, org_id)` returns `dict[UUID, tuple[int, str | None]]` mapping each pr_id to `(count, max_severity)` in one batch query (used by the tickets list).
- Generation-2 plan §10.13 metrics — `compute_acceptance_rate(*, org_id)` (`resolved_confirmed + resolved_unverified` / total), `compute_resolved_without_edit_rate(*, org_id)` (`acknowledged + resolved_unverified + stale` / total).
- Generation-2 save-time hooks — `dispatch_events(aggregate)` drains the aggregate's pending events to the `core/events` bus via a `_DomainEventEnvelope` adapter; `dispatch_audits(aggregate, *, session, actor, org_id)` writes one `audit_entries` row per finding state-changing event (`FindingRaised`, `FindingReObserved`, `FindingAnchorUpdated`, `FindingStateChanged`, `FindingAcknowledged`, `FindingResolutionDetected`, `FindingStaleDetected`). Service-layer callers invoke both after every `agg_repo.save(aggregate)`.
- Generation-2 events — `ReviewRequested`, `ReviewStarted`, `ReviewCompleted`, `ReviewFailed`, `ReviewSuperseded`, `FindingRaised`, `FindingReObserved`, `FindingStateChanged`, `FindingAcknowledged`, `FindingResolutionDetected`, `FindingStaleDetected`, `FindingAnchorUpdated`, `CommentReplyReceived`, `AgentReplyPosted`, plus `DomainEvent` union.

HTTP routes (`/api/reviewer`):

- `POST /rereview` — body `{ ticket_id }`; UI button. Starts a `pr_review_v1` workflow execution via the engine.
- `POST /cancel?ticket_id=…` — cancels every non-terminal workflow execution for the ticket via `workflow.request_cancel`.
- `GET /jobs/by-ticket/{ticket_id}` — workflow_executions for the ticket's PR, projected into `ReviewJob` shape via `workflow_review_view`. Newest first.
- `GET /findings/by-ticket/{ticket_id}?include_terminal=…` — list open + acknowledged findings. Set the query param to `true` to also return resolved + stale.
- `GET /conversations/by-ticket/{ticket_id}` — All-Conversations cross-cut: findings whose thread has ≥1 developer (`author_kind='human'`) message. Terminal-state findings (resolved_*, stale) are excluded. Findings yaaos raised but the developer never replied to don't appear here — the per-review timeline already surfaces those.
- `GET /metrics` — aggregate counters, projected from `workflow_executions`.

No `on_startup` hook — the workflow engine has its own cleanup loop.

## Module architecture

### File layout

Module layout:

| Module | Responsibility |
|---|---|
| `__init__.py` | Public entry points — `start_pr_review`, `cancel_workflows_for_ticket`, plus generation-2 surface (aggregate, types, helpers). |
| `web.py` | HTTP routes (`/rereview`, `/cancel`, `/jobs/by-ticket`, `/findings/by-ticket`, `/conversations/by-ticket`, `/reviews/by-ticket`, `/threads/by-finding`, `/metrics`). |
| `incremental.py` | `handle_push` — auto-incremental review runner. Owns the trigger-policy decision + spawns a self-driving runner that writes through the aggregate. |
| `workflow_review_view.py` | Projects `workflow_executions` rows into the `ReviewJob` shape that `/jobs/by-ticket` + `/metrics` consume. |
| `review_job.py` | `ReviewJob` + `ReviewJobInput` Pydantic value objects (SPA-facing shape). |
| `secrets_detection.py` | `detect_secrets(diff)` + `secrets_warning_review(rule_id)`. Pure regex pre-flight. |
| `mcp_wiring.py` | `build_mcp_payload`, `prefix_broken_creds_warning`. MCP-provider collection + the broken-creds GitHub callout. |
| `diff_utils.py` | `detect_language`, `ticket_skip_reason`, `is_skip_path`. Pure `Diff` inspection. |
| `constants.py` | `REVIEWER_TAG`, `CODING_AGENT_PLUGIN_ID`, `DEFAULT_MODEL`, `DEFAULT_EFFORT`. |
| `admission.py` | `admit_raw_findings`, `findingdrafts_to_raw`, `raw_to_vcs_findings`, `post_admitted_findings_to_vcs`. The `PostFindings` command + `incremental.py` both import from here. |
| `commands/__init__.py` | `WorkflowCommand` bodies (5 Workspace + 5 Local). |
| `workflows/*` | `Workflow` definitions for the 5 reviewer task modes. |
| `aggregate.py` + `repository.py` + `events.py` + `types.py` + `models.py` | Durable-findings layer. |
| `service.py` + `replies.py` + `trigger.py` + `lock.py` + `anchor.py` | Reply classifier, trigger policy, advisory locking, anchor resolution. |
| `orphan_sweep.py` | Periodic safeguard: tickets stuck `running` past the grace window with no `reviews` row → `tickets.fail(reason='orphaned_no_review_job')`. Spawned via the module's `on_startup` hook. Cadence + grace from `yaaos_ticket_orphan_{sweep_interval,grace}_seconds` (defaults 60 s / 300 s). |

### Workflows + commands

Five typed `Workflow` definitions live in `domain/reviewer/workflows/` and register at module import:

- `pr_review_v1` — `CheckShouldReview → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`.
- `incremental_review_v1` — same shape with `IncrementalReview` substituted.
- `verify_fix_v1` — `ProvisionWorkspace → VerifyFix → ResolveFinding → CleanupWorkspace`.
- `stale_check_v1` — `ProvisionWorkspace → StaleCheck → ArchiveStaleFindings → CleanupWorkspace`.
- `answer_question_v1` — `ProvisionWorkspace → AnswerQuestion → PostReply → CleanupWorkspace`.

Ten matching `WorkflowCommand`s ship with real bodies in `domain/reviewer/commands/`:

- Workspace category (5): `CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion` — each wraps a `domain/coding_agent` invocation against the resolved workspace.
- Local category (6): `CheckShouldReview` (admission gate before provisioning), `SecretsScan` (pre-flight secrets gate), `PostFindings`, `ResolveFinding`, `ArchiveStaleFindings`, `PostReply`.

The three workspace-lifecycle commands (`ProvisionWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth`) ship in [`core/workspace.commands`](core_workspace.md) and register through the reviewer bootstrap so any workflow can reference them.

All 10 reviewer command bodies (5 Workspace + 5 Local) carry real implementations.

- `CheckShouldReview` reads `is_draft` / `is_fork` / `labels` / `author_login` from the ticket payload and returns `Outcome.success(label="skip", outputs={"reason": ...})` on any first-match signal (`draft`, `fork`, `label:<name>`, `bot_author`). Skip labels: `yaaos-skip`, `no-review`, `wip` (case-insensitive). The bot-author check matches `*[bot]` / `*-bot` suffixes.
- `ArchiveStaleFindings` consumes `stale_finding_ids: list[str]` from inputs (sourced from the prior `StaleCheck` step), loads the reviewer aggregate by `pr_id` via the registered `WorkflowContextProvider`, and transitions each finding to `STALE` via `aggregate.record_stale_detection(still_applies=False, confidence=1.0)`. Defensive on missing pr_id (no-op-success), unknown finding ids (skipped, not failed), invalid uuids (skipped). Outputs `archived_count` and `skipped_count`.
- `ResolveFinding` consumes `verdict: dict` from inputs (sourced from the prior `VerifyFix` step). Parses `finding_id`, `still_present`, `confidence`; loads the aggregate by `pr_id`; calls `aggregate.record_fix_verification(...)` which transitions to `RESOLVED_CONFIRMED` only when `still_present=False` AND `confidence ≥ threshold` (default 0.80). Lower-confidence verdicts and `still_present=True` are no-ops — the finding stays open. Outputs `transitioned_to` (state value or None). Defensive on empty/missing verdict, invalid finding_id, invalid confidence, missing pr_id, unknown finding.
- `PostFindings` consumes `draft_findings: list[dict]` (FindingDraft-shaped) + `workspace_id` from inputs. Resolves the workspace and ticket context, deserializes the drafts via `FindingDraft.model_validate`, pre-fetches anchor-file contents via `workspace.read_text`, calls `findingdrafts_to_raw` → `admit_raw_findings` → `post_admitted_findings_to_vcs`. Outputs `admitted_count`, `dropped_count`, `posted`. Empty drafts → success-no-op. Admitted findings persist via admission AND post to the registered VCS plugin (typically GitHub) with thread/yaaos-message attachment in a single workflow step.
- `PostReply` consumes `reply_body` + `finding_id`. Loads the aggregate by `pr_id`, finds the thread, locates the first yaaos message's `external_comment_id` as the parent comment, calls `pull_requests.get` for the PR's external id, and calls `vcs.post_comment_reply(pr_external_id, parent_external_id, body)`. The returned external_comment_id is persisted on the new CommentMessage. When no real parent exists yet (e.g. PostFindings hadn't run for this finding) or no PR row, falls back to `local-reply-<uuid>` placeholder — same behavior as the pre-slice-32 state.

The five Workspace reviewer commands (`CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion`) share a base `_WorkspaceReviewCommand` that on every invocation:

1. Resolves `workspace_id` from inputs → live `Workspace` handle (failure on missing/invalid/unresolvable).
2. Fetches the ticket's `WorkspaceTicketContext` (org_id, plugin_id, repo, payload, pr_id) via the registered provider (failure on missing provider / missing ticket).
3. Forwards `(workspace, ticket_ctx, inputs, ctx)` to subclass `_run_in_workspace`.

Subclass bodies override `_run_in_workspace` to call the matching `domain/coding_agent.<method>`:

- `CodeReview` builds a minimal `VCSPullRequest` + empty `Diff` from `ticket_ctx.payload` and invokes `coding_agent.review`. Outputs `draft_findings` (FindingDraft-shaped dicts) + `summary_body` + `state` for `PostFindings`.
- `IncrementalReview` invokes `coding_agent.incremental_review` with `prev_sha = base_sha` (defaulting to `head_sha`).
- `VerifyFix` loads the finding by id via `SqlAlchemyAggregateRepository`, reads the current code snippet at the anchor via `workspace.read_text`, invokes `coding_agent.verify_fix`. Outputs `verdict` for `ResolveFinding`. Unknown finding → success with `skipped="unknown"` (not a workflow failure).
- `StaleCheck` loops over `finding_ids`, reads each anchor's current snippet, invokes `coding_agent.stale_check`, accumulates ids whose verdict is `still_applies=False AND confidence ≥ 0.80`. Outputs `stale_finding_ids` for `ArchiveStaleFindings`. Per-finding failures are logged and skipped, not propagated.
- `AnswerQuestion` loads the finding, reads its anchor snippet, invokes `coding_agent.answer_question`, outputs `reply_body` + `finding_id` for `PostReply`. Empty/unknown inputs → success-no-op.

Tests register a fake plugin via [`testing/fake_coding_agent`](testing_fake_coding_agent.md) (standalone, doesn't wrap a real plugin) under `plugin_id="claude_code"` — the command bodies hardcode that id.

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
- `RawFinding` — coding-agent output before admission; must include `concrete_failure_scenario` ≥ 20 chars or the aggregate drops it. Built from `coding_agent.FindingDraft` via `findingdrafts_to_raw` in [`admission.py`](../app/domain/reviewer/admission.py); admitted survivors are translated back to `vcs.Finding` for posting via `raw_to_vcs_findings`. These two are the only shared converters.
- `AdmissionDrop` — audit-log payload for a rejected raw finding: `(rule_id, reason, severity, confidence)` where reason ∈ `malformed | below_threshold | nit_cap | top_cap | matches_ack`.

### Core user flows

#### Full-review flow — `pr_review_v1` (engine path)

1. The github intake type's PR-opened branch (or `/yaaos full review` / `@yaaos rereview` on an existing PR) calls `start_pr_review(ticket_id, org_id=, trigger_reason=)`, which resolves the ticket via the registered `WorkflowContextProvider` and starts a `pr_review_v1` workflow execution via `core/workflow.engine.start(...)`.
2. The engine routes the workflow step by step: `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`. See [`commands/__init__.py`](../app/domain/reviewer/commands/__init__.py) for each command body and [`workflows/`](../app/domain/reviewer/workflows/) for the typed step lists.
3. `CodeReview` resolves the workspace, fetches the ticket context, builds an MCP payload via `mcp_wiring.build_mcp_payload`, invokes `coding_agent.review(plugin_id="claude_code", ...)`, and emits `draft_findings` + `summary_body` + `state` for `PostFindings`. The `on_activity` callback publishes `ActivityEvent`s to `core/sse_pubsub` channel-per-workflow-execution.
4. `PostFindings` deserializes the drafts, pre-fetches anchor contents, runs admission via `findingdrafts_to_raw` → `admit_raw_findings`, and calls `post_admitted_findings_to_vcs` (which posts each survivor via the registered VCS plugin and persists per-finding `external_comment_id` on the aggregate). Rejected drafts never reach GitHub.
5. `CleanupWorkspace` runs as the workflow's `final` step regardless of upstream success/failure; the engine's cleanup-failsafe ensures workspace teardown even on crashes.

#### Durable findings layer

1. **Initial review on PR ready**: service acquires the per-PR advisory lock, opens a transaction, loads the aggregate, starts a `Review` via `start_review`, invokes coding_agent in `full_review` mode, maps each `FindingDraft` → `RawFinding`, calls `aggregate.post_process_raw_findings` which applies the malformed / threshold / per-PR nit cap / cross-file dedup / per-review top-10 cap pipeline + dedup vs prior open/acknowledged findings. For each survivor: opens a thread, appends a yaaos message, posts via `vcs.post_review`. Completes the review; saves the aggregate; drains domain events.
2. **Auto-incremental on push**: intake hands `(pr_id, new_head, prev_head)` to a trigger-policy helper. `trigger.decide_trigger` returns `Skip | Debounce | Run`. On `Run`, the service schedules a debounced incremental review whose worker (a) invokes `coding_agent.incremental_review` on `prev_sha..head` and (b) runs the deterministic anchor pass `resolve_open_anchors(aggregate, *, touched_files, read_file, new_commit_sha)` (in `incremental.py`) BEFORE any LLM stale_check. That pure helper returns a `ResolveAnchorsResult` partitioning open findings into `moved` / `gone` / `unchanged`; `gone` transitions to `resolved_unverified`, `moved` is fed into `coding_agent.verify_fix` and routed through `apply_verify_fix_result`, `unchanged` stays put. New findings flow through the same admission pipeline. Anchor mutations happen on a snapshot aggregate loaded inside the workspace block (no long-lived DB session); the moves are replayed onto a freshly-loaded live aggregate at save time.
3. **Developer reply**: intake routes a `pull_request_review_comment` / `issue_comment` payload through a thread-resolution layer that finds the `CommentThread` by `external_thread_id`. The service runs the deterministic checks first — `is_yaaos_command` routes to the command handler, `is_off_topic_message` stores without classifying. Otherwise `classify_reply` runs through `core/llm` and emits exactly one of five categorical intents; `apply_classified_reply` maps that label 1:1 onto a `ReplyAction`:

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
- `acknowledged` (terminal today; `acknowledged → open` not yet implemented)
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

### Concurrency

`lock.acquire_pr_lock` issues `pg_advisory_xact_lock(hashtext('pr:<uuid>')::bigint)` inside the calling transaction. Two webhook events for the same PR serialize cleanly; the lock releases automatically at commit/rollback. Read-only entry points (`list_*` / `get_*`) do NOT take the lock.

### Admission pipeline (plan §10)

Inside `aggregate.post_process_raw_findings(review_id, raw, *, diff_files=None)`, in order:

1. **Schema gate** — drop raw findings whose `concrete_failure_scenario` is missing or under `_MIN_SCENARIO_LEN` (20 chars stripped). Audit reason: `malformed`.
2. **Off-diff drop** — when `diff_files` is supplied (`commands/__init__.py`'s `PostFindings` for full review, `incremental.py` for incremental), findings whose anchor file isn't in the PR diff are dropped (plan §10.9). Audit reason: `off_diff`.
3. **Per-severity threshold** — `blocker`/`major` ≥ 75, `minor` ≥ 85, `nit` ≥ 90 (plan §10.2). Audit reason: `below_threshold`.
4. **Per-PR nit cap** — at most 5 nits ever for this PR (plan §10.5). Audit reason: `nit_cap`.
5. **Fingerprint match** — vs prior findings on this PR: matches against `acknowledged` drop silently (`matches_ack`); matches against `open` re-observe with sticky severity + `max(stored, new)` confidence.
6. **Cross-file dedup** — same-rule findings on multiple files collapse into one survivor whose body gains an "Also in: file2, file3, …" footer enumerating the duplicated paths (plan §10.8).
7. **Per-review top-10 cap** — rank by `severity_weight × confidence` (blocker=4, major=3, minor=2, nit=1); admit top 10. Re-observations don't count. Audit reason: `top_cap`.

Admission runs BEFORE `vcs.post_review` in the `PostFindings` command — rejected drafts never reach GitHub.

### Cancellation

`cancel_workflows_for_ticket(ticket_id)` walks every non-terminal `workflow_executions` row for the ticket and calls `workflow.request_cancel` on each. The engine flips `cancel_requested=True`; at the next step boundary the workflow transitions to `cancelled` and the workspace gets torn down via the cleanup-failsafe. Intake's PR-closed handler + `/yaaos cancel` comment + the SPA's `/cancel` endpoint all route through this single helper.

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

`SqlAlchemyAggregateRepository.save` flushes in FK order: findings → flush → observations + threads → flush → messages → flush → acks. It also persists `Review` row updates (status, `commit_sha_at_start`, `superseded_by_review_id`, `pending_replay`, `started_at` / `completed_at` timestamps) via `_review_from_row` — initial `ReviewRow` INSERT still lives in `queue.py` (the schedule API) / `incremental.py` because those callers hold the per-PR advisory lock and assign `sequence_number`.

`review_id` columns on generation-2 tables are unconstrained UUIDs by design; the `review_jobs → reviews` rename in §13 step 7 turns them into a real FK. Canonical schema in [core_database.md](core_database.md).

## How it's tested

- **Unit tests** for `state_machine.py`, `fingerprint.py`, `anchor.py`, `trigger.py`, the aggregate, the service helpers (`apply_classified_reply` / `apply_verify_fix_result` / `apply_stale_check_result` / `is_yaaos_command` / `is_off_topic_message`), and the classifier (with a canned-output runnable substituting `core/llm`).
- **In-memory `AggregateRepository`** at `test/in_memory_repository.py` exercises the admission pipeline (threshold, nit cap, top cap, cross-file dedup, fingerprint match vs prior open/acknowledged), state transitions, and round-trip persistence.
- **Integration coverage** for generation 1 (`test_detect_secrets.py`, plus the scheduling / supersession / handler / startup-recovery suites in `app/test/` + `apps/e2e/`) gates the generation-1 flow.
- **Service tests** (`@pytest.mark.service`, see [patterns.md § Testing](patterns.md)): `test_pr_review_v1_e2e_service.py` drives the full `pr_review_v1` pipeline in-process using `app/testing/stub_vcs` + stub coding-agent + stub workspace. `test_mcp_review_pipeline_service.py` composes the MCP proxy + broken-creds tracker + review-output prefix. `test_secrets_scan_service.py` covers the `SecretsScan` Local command refusing to provision a workspace when the diff carries leaked secrets. `test_cancel_dual_write_service.py` covers `/api/reviewer/cancel` flipping non-terminal workflow executions via `request_cancel`. `test_all_workflows_smoke.py` exercises every one of the 5 reviewer workflows end-to-end via the engine.
- **Evals** for the `classify_reply` prompt live under `domain/reviewer/eval/` (one `.eval.py` per prompt + fixtures); evals deliberately bypass `langchain.cache.SQLiteCache` so they always hit the model fresh.
