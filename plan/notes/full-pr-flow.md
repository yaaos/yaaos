# Reviewer module: implementation plan

> Implementation plan for re-architecting `apps/backend/app/domain/reviewer` to handle multiple reviews per PR, durable findings with a state machine, persistent acknowledgments, and developer reply handling. Source for the eventual module doc.

## 1. Context

Today the reviewer module is shallow:
- One `ReviewJob` per run; findings live as a JSONB array on the row.
- The UI shows only the latest job.
- Developer replies on findings are received by webhook and silently dropped (per the explicit deferral comment in `apps/backend/app/domain/intake/service.py:235`).
- Every push to a PR fires a full re-review.

We need:
- Multiple reviews per PR, displayed historically.
- `Finding` as a first-class entity that persists across reviews, with a state machine.
- Persistent acknowledgment decisions (intentional/wontfix) that survive future reviews.
- Developer reply handling — intent classification (direct LLM call), plus code-touching follow-ups (verify-fix, stale-check, respond) routed through `coding_agent`.
- Auto-incremental review on push (debounced); manual full review only.
- A new `core/llm` module (built first; see §14) supporting the one direct LLM call (`classify_reply`). All other prompts run through `coding_agent`.

This plan establishes the domain model, schema, internal architecture, and external surfaces. It is **maximalist** — the module doc that ships will be terser.

## 2. Domain model (DDD)

### 2.1 Aggregate

**`PRReviewAggregate`** — single consistency boundary per PR. Root = `PullRequest`. Owns: all `Review`s, all `Finding`s, all `FindingObservation`s, all `CommentThread`s + `CommentMessage`s, all `AcknowledgmentDecision`s for that PR.

Reason: re-review must atomically read prior findings + their acks to avoid re-raising, and write the new review + observations together. One PR ≈ one transaction.

External callers never touch a `Finding` directly. Everything goes through the aggregate.

### 2.2 Entities

| Entity | Identity | Lifecycle |
|---|---|---|
| `PullRequest` | (repo_id, pr_number) | Long-lived; created on intake. |
| `Review` | UUID + per-PR `sequence_number` (1, 2, 3, …) | One run. Append-only history. Sequence used for UI labels. |
| `Finding` | UUID; matched-across-runs via `FindingFingerprint` | Durable per PR; state machine. |
| `FindingObservation` | UUID | One row per (finding, review) pair. Append-only. |
| `CommentThread` | UUID; 1:1 with `Finding` | Created when first comment posted. |
| `CommentMessage` | UUID | One per posted/received comment. |
| `AcknowledgmentDecision` | UUID | Created when developer acknowledges; survives all future reviews. |

### 2.3 Value objects (immutable)

- **`FindingFingerprint`** — `(file_path, rule_id, anchor_content_hash, body_gist_hash)`. The conceptual identity across reviews. Two raw findings with the same fingerprint = the same `Finding`.
  - `anchor_content_hash` — SHA-256 of the **line content** at `line_start..line_end`, whitespace-normalized (collapse runs of whitespace to single space, strip trailing).
  - `body_gist_hash` — SHA-256 of the normalized **rule_id + title** (lowercased, whitespace-collapsed). Body text varies between runs (model phrasing); title + rule are stable enough to dedupe.
- **`CodeAnchor`** — `(file_path, line_start, line_end, surrounding_content_hash, commit_sha)`. Enables re-resolving a finding to a new line after the file changes.
  - `surrounding_content_hash` — SHA-256 of 3 lines before `line_start` + the anchored line range + 3 lines after `line_end`, whitespace-normalized. Used to re-find the anchor when line numbers drift.
- **`Severity`** — `blocker | major | minor | nit`.
- **`FindingState`** — `open | acknowledged | resolved_confirmed | resolved_unverified | stale`. (POC drops `superseded` — see §11.)
- **`AckKind`** — `intentional | wontfix`. (POC set. `intentional` = "this is by design"; `wontfix` = "real but we're not changing it.")
- **`ReplyIntent`** — POC set: `acknowledgment | verify_fix | other`. Anything not in this set falls under `other`. Future expansion: `question | pushback | alternative_request | correction | off_topic`.
- **`ReviewTrigger`** — tagged union: `PRReady | PushIncremental{prev_sha} | ManualFull`. (POC set.)
- **`ReviewScope`** — `Full(base..head)` or `Incremental(prev_sha..head)`.
- **`AuthorKind`** — `yaaos | human`.

### 2.4 Domain events

- `ReviewRequested(trigger, scope)`
- `ReviewStarted(commit_sha)`
- `ReviewCompleted(findings_observed: list[finding_id])`
- `ReviewFailed(reason)`
- `ReviewSuperseded(by_review_id)`
- `FindingRaised(finding_id)` — fingerprint new to this PR.
- `FindingReObserved(finding_id, review_id)` — fingerprint seen again.
- `FindingAnchorUpdated(finding_id, new_anchor)`
- `FindingStateChanged(finding_id, from, to)`
- `FindingAcknowledged(finding_id, ack_id, kind)`
- `FindingResolutionDetected(finding_id, kind=confirmed|unverified)`
- `FindingStaleDetected(finding_id)`
- `CommentReplyReceived(thread_id, message_id, classified_intent)`
- `AgentReplyPosted(thread_id, message_id)`

## 3. State machine — `FindingState`

States: `open`, `acknowledged`, `resolved_confirmed`, `resolved_unverified`, `stale`. New findings enter `open`.

| From → To | Trigger |
|---|---|
| (new) → `open` | New fingerprint observed in a review. |
| `open` → `acknowledged` | Developer reply classified as `acknowledgment` with sufficient confidence. Carries an `AckKind` (`intentional` or `wontfix`). |
| `open` → `resolved_confirmed` | Coding-agent `verify_fix` returns "not present" with confidence ≥ threshold. |
| `open` → `resolved_unverified` | Anchor gone in new commit and no verify-fix possible. |
| `open` → `stale` | Coding-agent `stale_check` returns "no longer applies" with confidence ≥ threshold. |
| `resolved_*` / `stale` | Terminal in this PR. |

**Hard rule:** the LLM/agent produces *evidence*; the aggregate decides the transition. Low-confidence output never causes a state change. Default fallback = leave `open`.

(`superseded` and `acknowledged → open` transitions are deferred — see §11.)

## 4. Database schema

### 4.1 New tables

**`findings`** — promotes findings from JSONB to first-class.
- `id` UUID PK
- `pr_id` FK
- `fingerprint_hash` (TEXT, indexed; computed from `(file, rule_id, anchor_content_hash, body_gist_hash)` — see §2.3)
- `rule_id` TEXT (e.g. `security/sql-injection`, `style/naming`, or null for free-form findings)
- `title` TEXT
- `body` TEXT
- `rationale` TEXT
- `concrete_failure_scenario` TEXT — required per §10.1; finding is dropped before insert if missing.
- `confidence` INT (0–100; severity-stickied across observations as max; see §10.10)
- `severity` TEXT (sticky across observations; never escalates)
- `state` TEXT (the `FindingState` enum)
- `current_anchor` JSONB (`CodeAnchor`)
- `source_agent` TEXT — opaque identifier of the agent that raised this finding, e.g. `coding_agent:full_review:v1`. Used for filtering and metrics.
- `first_seen_review_id` FK reviews
- `last_observed_review_id` FK reviews
- `created_at`, `updated_at`
- `org_id`
- Unique constraint on `(pr_id, fingerprint_hash)`.

**`finding_observations`** — append-only history of when a finding was seen.
- `id` UUID PK
- `finding_id` FK
- `review_id` FK
- `anchor` JSONB (`CodeAnchor` at the time)
- `raw_body` TEXT (what the agent actually said this run; may differ slightly between runs)
- `created_at`

**`acknowledgment_decisions`** — persistent dev decisions.
- `id` UUID PK
- `finding_id` FK
- `kind` TEXT (`AckKind`)
- `rationale` TEXT (developer's words, raw)
- `made_by_external_id` TEXT (GitHub user)
- `made_by_message_id` FK comment_messages
- `created_at`

**`comment_threads`** — 1:1 with `Finding`.
- `id` UUID PK
- `finding_id` FK UNIQUE
- `external_thread_id` TEXT (GitHub review thread id if available), indexed for webhook lookups
- `created_at`, `updated_at`

(No `status` column for POC — finding state already conveys lifecycle. Thread resolution remains a human action on GitHub.)

**`comment_messages`** — every message in every thread.
- `id` UUID PK
- `thread_id` FK
- `author_kind` TEXT (`yaaos | human`)
- `author_external_id` TEXT
- `external_comment_id` TEXT (GitHub comment id), indexed for webhook lookups
- `in_reply_to_external_id` TEXT (parent on GitHub side)
- `body` TEXT
- `classified_intent` TEXT (nullable; only for human messages)
- `classification_confidence` REAL (nullable)
- `created_at`

### 4.2 Changed tables

**`review_jobs`** — rename internally to `reviews`. Add:
- `sequence_number` INT — 1, 2, 3 per PR. Assigned at insert via `MAX(seq) + 1` for that pr_id, inside the per-PR advisory lock (§5.7). Used for UI labels and ordering.
- `trigger_reason` TEXT — POC values: `pr_ready | push_incremental | manual_full`.
- `scope_kind` TEXT (`full | incremental`)
- `scope_prev_sha` TEXT (nullable; for incremental)
- `commit_sha_at_start` TEXT
- `superseded_by_review_id` FK nullable — used when a manual full review cancels an in-flight incremental.
- `pending_replay` BOOL DEFAULT false — set when a push arrives during an in-flight review; on completion, the trigger policy re-evaluates and may schedule another review.

Unique constraint on `(pr_id, sequence_number)`.

**Remove**: `findings` JSONB column on `reviews`. Findings now live in their own table linked via `finding_observations`.

**Drop**: `posted_comments` table — subsumed by `comment_messages`.

### 4.3 Migration plan

One backfill migration:
1. Create new tables.
2. For each existing `review_jobs` row: for each finding in the JSONB array, create a `Finding` (fingerprint computed from the existing fields), a `FindingObservation`, and migrate the matching `PostedCommentRow` into `comment_messages` + `comment_threads`.
3. Drop the JSONB column and the `posted_comments` table.
4. Rename `review_jobs` → `reviews`.

POC pragmatism: if the existing data is throwaway (likely at M01), just drop and recreate. Easier than a backfill.

## 5. Module architecture (internal)

Proposed layout under `apps/backend/app/domain/reviewer/`:

```
domain/reviewer/
├── __init__.py              # Public service interface
├── service.py               # Application service (orchestration)
├── aggregate.py             # PRReviewAggregate (the consistency boundary)
├── repository.py            # SQLAlchemy implementation of the AggregateRepository Protocol
├── repository_protocol.py   # Protocol for the aggregate repository; in-memory impl in test/
├── models.py                # SQLAlchemy row models
├── types.py                 # Value objects, enums, dataclasses
├── fingerprint.py           # FindingFingerprint computation, anchor hashing
├── anchor.py                # CodeAnchor resolution (line drift handling)
├── state_machine.py         # FindingState transitions; pure functions
├── trigger.py               # Trigger policy (debounce, force-push, draft, base-merged)
├── lock.py                  # PG advisory-lock helper, per pr_id
├── llm/
│   ├── __init__.py
│   ├── classifier.py        # ReplyIntent classification (direct LLM call via core/llm)
│   └── prompts/             # Versioned prompt files for classifier + coding_agent task templates owned by reviewer
├── agent_tasks/
│   ├── verify_fix.py        # Builds task input/output schema for coding_agent verify_fix mode
│   └── stale_check.py       # Same for stale_check
├── routes.py                # HTTP routes
├── events.py                # Domain events + subscribers
├── test/                    # singular, project convention
└── eval/                    # singular; one .eval.py per prompt + fixtures/
```

(POC drops `respond_to_question` and `propose_alternative` agent_tasks per §11. Add them when reply intents beyond the 3-class POC are activated.)

### 5.1 Public interface (exposed from `__init__.py`)

- `schedule_review(ticket_id, trigger, actor, org_id) -> ReviewId | None` — resolves ticket → PR, schedules review on that PR.
- `cancel_pending(ticket_id, actor, org_id, reason) -> int`
- `handle_developer_reply(external_thread_id: str | None, external_comment_id: str, in_reply_to_external_id: str | None, body: str, author_external_id: str, org_id) -> None` — accepts GitHub-side identifiers; resolves to internal thread/finding via the indexed `external_*` columns; no-op if no thread match (not one of ours).
- `handle_push(pr_id, new_head_sha, prev_head_sha, org_id) -> None` — debounces, decides if incremental review fires.
- `list_reviews_for_pr(pr_id, org_id) -> list[ReviewView]`
- `get_review(review_id, org_id) -> ReviewView`
- `list_findings_for_pr(pr_id, org_id, include_terminal=False) -> list[FindingView]` — `include_terminal=False` (default) excludes `resolved_confirmed`, `resolved_unverified`, and `stale`. The All Conversations view calls this with the default. Resolved findings stay in the DB for future surfacing.
- `get_thread(thread_id, org_id) -> ThreadView`
- `startup_recovery()` (unchanged in shape)

### 5.2 Application service responsibilities

`service.py` orchestrates. It does **not** make domain decisions — the aggregate does. It:
- Receives external triggers (HTTP, webhooks, scheduler).
- Loads the aggregate.
- Calls aggregate methods.
- Persists.
- Dispatches domain events to subscribers.
- Invokes adapters (coding_agent, vcs, llm) at the right moments.

### 5.3 Aggregate responsibilities

`aggregate.py` is the brain. Pure-ish — takes data in, produces decisions + events out. Methods:
- `start_review(trigger, scope, commit_sha) -> Review`
- `record_observation(review, raw_finding) -> (Finding, Observation, events)` — handles dedupe, fingerprint matching, state transitions.
- `complete_review(review_id, observed_finding_ids) -> events`
- `apply_developer_reply(thread_id, classified_intent, message) -> events` — transitions finding state if warranted.
- `record_fix_verification(finding_id, verifier_result) -> events`
- `record_stale_detection(finding_id, stale_result) -> events`
- `findings_to_recheck_after_push(new_head_sha, diff) -> list[finding_id]` — anchor-affected open findings.

State-machine transitions live in `state_machine.py` as pure functions called by the aggregate.

### 5.4 Adapter boundaries

**Rule of thumb: anything that needs to look at code goes through `coding_agent`. Direct LLM calls are only for non-code text reasoning.**

- `coding_agent` (existing, gains new task modes; see §13 step 2):
  - `full_review` — existing.
  - `incremental_review` — new mode; reviews a commit-range diff.
  - `verify_fix` — given a finding + original code + current code, determine if the issue is still present.
  - `stale_check` — given a finding + current code at new anchor, determine if the finding still applies.
  - (Deferred for POC: `respond_to_question`, `propose_alternative`. See §11.)
  Each mode is a task type with its own prompt + structured output schema. Reviewer module supplies the prompt and context; coding_agent runs the agent loop.
- `vcs` (existing) — invoked to post comments and replies.
- `core/llm` (new module, built first per §13) — used **only** for non-code direct LLM calls (today: just `classify_reply`). Owns LLM-call mechanics (provider abstraction, structured output validation, retries, gateway routing). **Does not hold prompts** — prompts live in the owner domain module (reviewer in this case). See §14 for the full spec.

### 5.5 Where prompts live

- Reviewer-owned prompts (classification, plus task templates for each coding_agent mode listed above) live in `domain/reviewer/llm/prompts/`.
- Versioned as code. Reviewed in PRs.
- `core/llm` does not own prompts — it owns LLM-call mechanics only.

### 5.6 Module imports / tach

- `domain/reviewer` may import: `core/database`, `core/workspace`, `core/llm`, `domain/vcs`, `domain/pull_requests`, `domain/coding_agent`, `domain/tickets`.
- `domain/reviewer` must not be imported by other domains except via its public interface.

Run `apps/backend/bin/sync_modules` after the module shape lands.

### 5.7 Concurrency — PG advisory lock per PR

Every public entry point that mutates the aggregate acquires a transaction-scoped Postgres advisory lock keyed on `pr_id` before loading the aggregate:

- `pg_advisory_xact_lock(hashtext('pr:' || pr_id::text)::bigint)` at the start of the transaction.
- Lock released automatically at transaction end.
- Implemented in `lock.py`; called by `service.py` (not the aggregate, not the repository).

Effect: two webhook events for the same PR serialize cleanly. No optimistic-retry logic, no schema changes, no manual reasoning about race conditions inside the aggregate.

Read-only entry points (`list_*`, `get_*`) do not take the lock.

## 6. Flow specifications

### 6.1 Initial review on PR ready

1. Intake → `reviewer.schedule_review(trigger=PRReady)`.
2. Service acquires advisory lock on `pr_id`, creates a `Review` row with `sequence_number = MAX(seq)+1`, status=`queued`.
3. Worker picks up. **Before invoking the agent**: load target-repo convention files (CLAUDE.md, CONTRIBUTING.md, AGENTS.md if present) into the agent context per §10.11. Then call `coding_agent` in `full_review` mode with full base..head diff + the loaded conventions.
4. Agent returns raw findings, each conforming to §10.1 schema.
5. **Post-processing order** (in the aggregate):
   a. Reject any finding missing `concrete_failure_scenario` or below per-severity confidence threshold (§10.2).
   b. Apply the per-PR nit cap (§10.5).
   c. Compute fingerprints; cross-file dedupe (`duplicate_of_rule_ids`) merges into a single Finding with a file list (§10.8).
   d. Apply the per-review top-10 cap by `(severity_weight × confidence)` (§10.5).
   e. For each survivor: create `Finding` (state=`open`), `FindingObservation`, `CommentThread`, post yaaos comment via vcs, store `CommentMessage`.
6. Review status → `done`.

### 6.2 Push to PR (auto-incremental)

1. VCS webhook → intake → `reviewer.handle_push(pr_id, new_head, prev_head)`.
2. Trigger policy (`trigger.py`):
   - Is `prev_reviewed_sha` ancestor of `new_head`? If no → log, surface "history changed, run full review"; stop.
   - Is the new diff just a merge from base branch? → log, surface "base merged"; stop.
   - Is PR a draft? → stop.
   - Otherwise: schedule debounced incremental review (wait 30–60s for quiet; cancel/extend on new pushes within window).
3. After debounce, `schedule_review(trigger=PushIncremental, scope=Incremental(prev_reviewed_sha..new_head))`.
4. Two parallel operations begin within the review:
   - **(a) Review the new diff** — load target-repo convention files (per §10.11), then coding_agent runs in `incremental_review` mode on `prev_reviewed_sha..new_head` with the loaded conventions in context, produces raw findings.
   - **(b) Re-check open findings touched by the diff** — for each `open` finding whose anchor is in a file changed by the diff:
     - Resolve new anchor (re-find content in the new file).
     - If anchor gone → invoke coding_agent in `stale_check` mode → transition to `stale` or `resolved_unverified`.
     - If anchor moved → update anchor; invoke coding_agent in `verify_fix` mode → either stay `open` or transition to `resolved_confirmed`.
5. Aggregate merges results from (a) and (b). For each (a) raw finding, in this order:
   a. Reject malformed (missing `concrete_failure_scenario`) and below-threshold by §10.2.
   b. Apply per-PR nit cap (§10.5).
   c. Compute fingerprint. Match against existing PR findings:
      - Matches an `acknowledged` finding → drop silently.
      - Matches an `open` finding → record `FindingReObserved`; severity stays sticky; confidence = max(old, new); no new comment.
      - No match → candidate for posting.
   d. Cross-file dedupe (§10.8) on the unmatched candidates.
   e. Apply per-review top-10 cap (§10.5) on the unmatched candidates only — re-observations don't count toward the cap.
   f. Create `Finding` (state=`open`), `FindingObservation`, `CommentThread`, post yaaos comment.
6. Review status → `done`. If `pending_replay = true` on this review, trigger policy re-evaluates and may schedule another incremental. UI surfaces a summary: N new, M re-observed, K resolved.

### 6.3 Manual full review

1. UI button or PR comment `@yaaos full review` → `reviewer.schedule_review(trigger=ManualFull, scope=Full(base..head))`.
2. Cancel any in-flight incremental review.
3. Otherwise same as 6.1, but with the deduplication / acknowledgment-respecting logic from 6.2 step 5 applied.

### 6.4 Developer reply

1. VCS webhook (`pull_request_review_comment`, `issue_comment`) → intake → `reviewer.handle_developer_reply(external_thread_id, external_comment_id, in_reply_to_external_id, body, author_external_id, org_id)`. Intake resolves to our internal `thread_id` via the indexed `external_thread_id` column on `comment_threads`. If no match, no-op (not a thread we own).
2. Cheap deterministic checks first:
   - Is it a yaaos command (`@yaaos review`, `@yaaos full review`, `@yaaos cancel`)? → route to command handler, skip classification.
   - Off-topic heuristic: message < 5 words AND no question mark AND no fix-claim regex (`/fix(ed|ing)?|done|address(ed|ing)?|resolved/i`) → store message, do nothing else.
3. Classification LLM call (`llm/classifier.py`, direct LLM call via core/llm — no code context needed, just text reasoning):
   - Input: original finding body, prior thread messages, this new message, surrounding code at the anchor (small snippet).
   - Output: `{intent: "acknowledgment" | "verify_fix" | "other", confidence: 0..1, suggested_ack_kind?: "intentional" | "wontfix", parsed_claims?: {fixed_in_commit_sha?}}` (POC 3-class set per §2.3).
4. Aggregate applies the reply, based on classification confidence (rubric in §10.3):
   - **`acknowledgment`, confidence ≥ 0.85** → create `AcknowledgmentDecision(kind=suggested_ack_kind or 'intentional')`, transition finding → `acknowledged`. **Post a short yaaos reply** ("noted, will skip this in future reviews").
   - **`acknowledgment`, confidence 0.60–0.84** → do NOT transition. Post a confirmation reply: "Reading this as 'intentional / wontfix' — reply 'confirm' to acknowledge, otherwise treat as a question." Wait for the confirm reply (which arrives as another `handle_developer_reply`); on confirm (exact-text match), transition.
   - **`verify_fix`, confidence ≥ 0.85** → invoke coding_agent in `verify_fix` mode against current HEAD; aggregate transitions on the result (§6.5).
   - **`other` / low confidence (< 0.60) / verify_fix below 0.85** → store message, no agent action.
5. Always store the developer message + agent response (if any) as `comment_messages`, with the classifier's intent and confidence stamped on the developer row.

### 6.5 Verify-fix subflow

1. Invoke coding_agent in `verify_fix` mode. Inputs: original finding body, original anchor's code (from stored snippet), current code at the resolved anchor on HEAD, surrounding context as needed.
2. Output schema: `{still_present: bool, confidence: 0..1, reasoning: str, observed_line?: int}`.
3. Aggregate decision:
   - `still_present=false`, confidence ≥ threshold → state → `resolved_confirmed`; post "confirmed fixed" reply. **Do not** auto-resolve the GitHub thread — leave that to the human. The reply is the signal.
   - `still_present=true`, confidence ≥ threshold → post "still see the issue at line N because …" reply; finding stays `open`.
   - Low confidence → post "unclear if fixed, please clarify" reply; stay `open`.

## 7. Trigger policy (debouncing & exclusions)

Centralized in `trigger.py`. Pure function returning a decision:

```
TriggerDecision = Skip(reason) | Debounce(seconds) | Run(scope)
```

Inputs: PR state, last_reviewed_sha, head_sha, in-flight reviews, recent push timestamps, PR draft state, list of new commits.

Rules (POC), evaluated in order — first match wins:
1. PR is draft → `Skip(draft)`.
2. Last reviewed SHA not ancestor of head → `Skip(history_changed)` — require manual.
3. New commits include a merge commit from base branch → `Skip(base_merged)` — require manual.
4. Existing in-flight review for this PR → side-effect: set `reviews.pending_replay = true` on the in-flight row; return `Skip(in_flight)`. When that review completes (§6.2 step 6), the trigger policy re-runs and may schedule a new incremental.
5. Push within debounce window (30s default) → `Debounce(window - elapsed)`.
6. Otherwise → `Run(Incremental(last_reviewed_sha..head))`.

Manual full review bypasses all of this and cancels any in-flight review (sets `superseded_by_review_id`).

## 8. Agent & LLM call inventory

Split by adapter. Prompts for every entry below live in `domain/reviewer/llm/prompts/`.

### 8.1 Direct LLM calls (via `core/llm`)

Only one. Pure text classification; no code context beyond a small snippet.

| Call | Purpose | Input | Output |
|---|---|---|---|
| `classify_reply` | Intent of developer message | finding body, thread context, new message, small code snippet at anchor | `{intent, confidence, suggested_ack_kind?, parsed_claims?}` |

### 8.2 Coding-agent task modes (POC set)

Everything that needs to read code goes through `coding_agent`. Each mode is a task type with a prompt + a strict output schema.

| Mode | Purpose | Input | Output |
|---|---|---|---|
| `full_review` | Review full base..head diff | diff, file contents, repo context, prior acknowledgments | `list[FindingDraft]` (each must match §10.1 schema) |
| `incremental_review` | Review prev_sha..head diff | diff, file contents, prior findings, prior acknowledgments | `list[FindingDraft]` |
| `verify_fix` | Is the finding still present? | original finding, original code snippet, current code at anchor, surrounding context | `{still_present, confidence, reasoning, observed_line?}` |
| `stale_check` | Does this finding still apply? | original finding, current code, what changed | `{still_applies, confidence, reasoning}` |

(Deferred: `respond_to_question`, `propose_alternative` — see §11.)

### 8.3 Discipline (all calls)

- Versioned prompt files in `domain/reviewer/llm/prompts/`.
- Structured output; validate; one retry on malformed; then drop with audit log.
- Hardcoded confidence thresholds per call type for POC (see §10).
- Eval fixtures and runners under `domain/reviewer/eval/` (singular).
- **Never let an LLM/agent call mutate state directly.** It returns evidence; the aggregate transitions.

## 9. UI surface

The review tab on the ticket detail page is restructured into two parallel views: a per-review timeline and a cross-cutting conversations view.

### 9.1 Page layout

```
┌─ Ticket detail / Review tab ────────────────────────────────┐
│ ┌─ Summary strip ──────────────────────────────────────────┐│
│ │ Open findings: 7   Acknowledged: 3   Resolved: 12        ││
│ │ Latest review: Review 4 (incremental, 2 min ago)         ││
│ └──────────────────────────────────────────────────────────┘│
│                                                              │
│ ┌─ All Conversations ─────────────────────────── [collapse]┐│
│ │ • Finding F-12 · open · 2 replies · Review 3            ││
│ │ • Finding F-09 · open · 1 reply · Review 2              ││
│ │ • Finding F-04 · acknowledged · "intentional" · Review 1 ││
│ │ … (only findings with ≥1 dev reply, or open threads)    ││
│ └──────────────────────────────────────────────────────────┘│
│                                                              │
│ ┌─ Review 4 (latest) ─────────────────────────── [expanded]┐│
│ │ trigger: push_incremental · sha abc123 · 2 min ago      ││
│ │ summary: 1 new, 3 re-observed, 2 resolved               ││
│ │ findings: [F-15 new] [F-12 re-observed] …               ││
│ └──────────────────────────────────────────────────────────┘│
│                                                              │
│ ┌─ Review 3 ─────────────────────────────────── [collapsed]┐│
│ │ trigger: push_incremental · 2 open threads · 1 hr ago   ││
│ └──────────────────────────────────────────────────────────┘│
│                                                              │
│ ┌─ Review 2 ─────────────────────────────────── [collapsed]┐│
│ └──────────────────────────────────────────────────────────┘│
│                                                              │
│ ┌─ Review 1 (initial) ───────────────────────── [collapsed]┐│
│ └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

### 9.2 Per-review sections

- One collapsible section per `Review`, newest at top.
- **Only the latest review is expanded by default; all older reviews are collapsed.** Native `<details>`.
- Collapsed header shows: review number (from `sequence_number`), trigger reason, age, *and* count of any still-open threads on findings first observed in that review (so buried-but-active items remain visible). Re-observation subnotes on older-review finding rows are surfaced upward via the "All Conversations" cross-cut (§9.3), so collapse doesn't hide active work.
- Expanded body shows:
  - Metadata: trigger, scope (full / incremental + prev_sha..head), commit_sha_at_start, status, model/tokens/duration, summary counters (`N new`, `M re-observed`, `K resolved`).
  - Findings *first observed in this review*. Re-observations don't duplicate the row — they add a small "seen again in Review N" subnote on the original row.
  - Each finding row: severity pill, title, state pill (`open` / `acknowledged` / `resolved` / `stale` / `superseded`), expand toggle to reveal body + rationale + anchor + thread.

### 9.3 Cross-cutting "All Conversations" section

- Top of page, above the per-review list. Collapsible; default open.
- Lists every finding that has either (a) ≥1 developer reply or (b) is `open` and was first raised more than 1 review ago. Excludes terminal states (`resolved_confirmed`, `resolved_unverified`, `stale`) — those stay in the DB but are not surfaced in POC. May be added later without a backfill.
- Each row: finding ID, current state, short title, last message preview, which review first raised it.
- Click → jumps to the finding in its origin review (auto-expanding that review).
- Reason this exists: when older reviews are collapsed by default, active conversations attached to old-review findings would otherwise be buried. This is the global "what needs my attention" view.

### 9.4 Threads inside findings

- Thread renders inside the expanded finding (in both the per-review view and when reached from All Conversations).
- Messages chronological. yaaos vs human styled differently. Show classified intent on human messages as a small tag.
- Read-only. Developer replies happen on GitHub; the UI syncs via webhook.
- Acknowledgment, when present, renders as a pinned banner above the thread: "Acknowledged as intentional — <rationale> — by @user on <date>."

### 9.5 Initiating reviews from the UI

- Two buttons at the top of the review tab:
  - **Re-review (incremental)** — disabled if nothing has changed since last review.
  - **Full re-review** — always enabled; warns before kicking off (cost / time).
- Also accept PR comments (canonical set): `@yaaos review` (incremental), `@yaaos full review`, `@yaaos cancel`. Same set is honored by `handle_developer_reply` (§6.4).

## 10. Noise control & confidence rubrics

Quality of findings is the single biggest determinant of whether the system gets muted within two weeks. These rules are load-bearing, not stylistic. Source: `plan/notes/ai-code-review-noise-control.md`.

### 10.1 Required output schema for every finding

Every raw finding produced by `coding_agent` (in any review mode) **must** include all of:

- `severity`: one of `blocker | major | minor | nit` — required.
- `rule_id`: short stable id (`security/sql-injection`, `correctness/null-deref`, `style/naming`, …). Required.
- `title`: one-line summary.
- `body`: short explanation.
- `concrete_failure_scenario`: required free text. Must describe inputs, code path, and observed-vs-expected behavior. **If the model cannot fill this, the finding is dropped before posting.** ("Prove it or discard it.")
- `confidence`: integer 0–100 (rubric below).
- `anchor`: `CodeAnchor`.
- `rationale`: why this is a problem.
- `duplicate_of_rule_ids` (optional): if same root issue appears in other files, list them; aggregate de-duplicates into a single comment with file list.

Malformed findings are rejected before they reach the aggregate (one repair attempt, then dropped with an audit log entry).

### 10.2 Finding confidence rubric (0–100)

Calibrated meaning, not a vibe. Reviewer prompt includes this rubric verbatim.

| Score | Meaning |
|---|---|
| **90–100** | Concrete reproducing scenario described. Failure is obvious to a senior engineer reading the diff. Would bet money on it. |
| **75–89** | Strong evidence. Specific failure scenario is plausible and traced through the code; not fully reproduced. High likelihood. |
| **60–74** | Plausible. Pattern matches a known bug class; specific failure path not proven. Reasonable people might disagree. |
| **40–59** | Speculative. Could be an issue under unusual conditions. Reviewer wouldn't bet on it. |
| **0–39** | Vibe-based pattern match. Drop before posting. |

**Post threshold (POC, hardcoded):**
- `blocker`: post at ≥ 75.
- `major`: post at ≥ 75.
- `minor`: post at ≥ 85.
- `nit`: post at ≥ 90, AND only if the total finding count is below the cap (see 10.5).

The asymmetry is intentional — nits need to clear a higher bar because they cause the most noise.

### 10.3 Classification confidence rubric

For `classify_reply`. Hardcoded thresholds:

| Score | Action |
|---|---|
| ≥ 0.85 | Act on the classification (transition state, route to handler). |
| 0.60–0.84 | Treat as `question` regardless of declared intent; do not transition state silently. For declared acknowledgments at this band, post a confirmation reply asking the developer to confirm before transitioning. |
| < 0.60 | Store message, no action. |

### 10.4 Verify-fix / stale-check confidence rubric

For `verify_fix` and `stale_check` (both coding_agent modes):

| Score | Action |
|---|---|
| ≥ 0.80 | Act (transition to `resolved_confirmed` / `stale`, post reply). |
| 0.50–0.79 | Post the observation as a non-resolving reply; leave finding state unchanged. |
| < 0.50 | No reply; leave finding state unchanged; log for review. |

### 10.5 Caps and ranking

Applied in this order during the aggregate's post-processing of raw findings:

1. **Per-PR nit cap (5 total ever posted)**: drop nits beyond the cap regardless of confidence. Applied first because it's a hard global limit.
2. **Per-review top-10 cap by `(severity_weight × confidence)`** where weights are `blocker=4, major=3, minor=2, nit=1`. Applied to the *unmatched* (new) candidates only — re-observations of existing findings don't count toward the cap.

Findings beyond the caps are dropped (not stored) with an audit log entry. Forces ranking.

### 10.6 "Do NOT flag" list (in prompt)

The reviewer prompt must include an explicit negative list. Initial list:

- Style or formatting that the project's linter/formatter handles.
- Naming preferences ("consider renaming X to Y") unless it obscures meaning.
- Missing comments / docstrings.
- "Consider using <library>" / architectural opinions on existing patterns.
- Speculative risks with no concrete failure scenario.
- Performance suggestions without measurement or a clear hot path.
- Anything already flagged by the linter/typecheck/security-scanner pre-pass (see 10.7).
- Anything matching an `AcknowledgmentDecision` for this PR.
- Findings on lines not in the current diff (unless the diff causally implicates them — model must justify).

This list lives in `domain/reviewer/llm/prompts/` and is reviewed in PRs.

### 10.7 Defer to deterministic tooling — DEFERRED for POC

Future: run linter/typecheck/security scanners before coding_agent and pass results as an "already flagged" list. Requires per-repo tool configuration we don't have yet. The "do NOT flag" prompt list (§10.6) handles most of the overlap until then.

When this lands, place the pre-pass between trigger evaluation and coding_agent invocation in §6.1 step 3 / §6.2 step 4(a).

### 10.8 De-duplication across files

Same root issue surfacing in N files → one comment with a file list, not N comments. Coding_agent must emit `duplicate_of_rule_ids` when applicable; aggregate enforces the merge.

### 10.9 Off-diff comments

Suppressed unless the model explicitly justifies the off-diff anchor (e.g. "new caller introduced in diff breaks invariant at line 200"). Without a justification, drop.

### 10.10 Severity and confidence stickiness across re-reviews

When a finding is re-observed (same fingerprint):
- **Severity** is sticky — never escalates. A `nit` raised in review #1 and ignored stays a `nit` in review #2.
- **Confidence** updates to `max(stored, new_observation)` — re-observing the same issue with higher confidence is useful for ranking; lower confidence is ignored.

Avoids the "naggy reviewer" failure mode while keeping the cap-ranking signal fresh.

### 10.11 Target-repo conventions are the source of truth

The reviewer prompts are **fully generic** — no language, framework, or yaaos-specific content. Anything project-specific comes from the **target repo's own convention files** loaded at review time:

- Primary: the target repo's `CLAUDE.md` (or equivalent — `CONTRIBUTING.md`, `AGENTS.md`, etc.). If present, its content is injected into the coding_agent context as authoritative project rules.
- If multiple convention files exist, all are loaded in a defined precedence (repo root first, then per-app or per-module overrides if the target repo organizes them that way).
- If none exist, the reviewer falls back to its generic rules only.

The reviewer treats these conventions as overriding defaults. Example: if the target repo's CLAUDE.md says "no defensive validation at internal boundaries," the reviewer must not flag missing input validation on an internal function. If it says "every public function needs a docstring," missing docstrings become valid `minor` findings.

This is recorded in §6.1 / §6.2 as a context-loading step before coding_agent is invoked.

### 10.12 Reviewer voice

Warm but concise. The reviewer is a helpful colleague, not a stern auditor or a chatty assistant. Three rules:

- One short paragraph per finding. No preamble. No "I noticed that..."
- Direct second-person where appropriate ("you can use X here") — softer than passive voice.
- No emoji, no exclamation points, no apologies. No filler ("hope this helps").

Generic example tone — short, actionable, not robotic:

> `foo()` can raise `KeyError` here when `bar` is missing from the dict. The caller doesn't catch it, so the request will 500. Consider `.get()` with a default, or catch and return a 400.

### 10.13 Eval metric for the reviewer itself

Tracked per finding type and rolled up per repo:

- **Resolved-without-edit rate** = (findings marked resolved by developer without a code change) / (findings posted). Higher = more noise.
- **Acceptance rate** = (findings that led to a code change) / (findings posted).
- **Tier mix**: ≥ 60% of posted findings must be `blocker` or `major` (Tier 1 / 2). If not, the reviewer is a noise generator.

These are POC observability targets; not enforced as gates yet. Log them and chart them.

## 11. POC simplifications (explicit cuts)

To keep M01 scope sane, defer:
- Cross-PR finding linking (refactor moves, etc.).
- Refactor-move detection (file A → file B).
- Full 7-way reply classifier — POC ships with `acknowledgment | verify_fix | other`. Other intents (`question`, `pushback`, `alternative_request`, `correction`, `off_topic`) collapse into `other` for now.
- `respond_to_question` and `propose_alternative` coding-agent modes.
- The full 4-way `AckKind` — POC ships with `intentional | wontfix` only. (`deferred`, `out_of_scope` deferred.)
- The `superseded` finding state — no implementation path for "later review raises a more specific finding at same anchor"; dropped until needed.
- Auto-resolving GitHub threads on confirmed fix. Agent posts the reply; human resolves.
- `acknowledged` → `open` transition (when code changes invalidate the ack). Treat all acks as durable for POC.
- Multi-author dedup (multiple humans replying to the same finding).
- Reply rate limiting / loop protection (other than "don't reply to your own reply").
- Linter/typecheck pre-pass (see §10.7).

## 12. Test plan

Lives under `domain/reviewer/test/` (singular).

- **Unit tests for `state_machine.py`** — every transition, every reject case.
- **Unit tests for `fingerprint.py`** — same content different lines → same fingerprint; different rule → different fingerprint; whitespace-only diff → same fingerprint.
- **Unit tests for `anchor.py`** — line drift cases.
- **Unit tests for `trigger.py`** — every rule, including `pending_replay` side effect on in-flight.
- **Unit tests for `lock.py`** — advisory-lock acquisition under contention.
- **Aggregate tests** with the in-memory implementation of `AggregateRepository` Protocol — full scenarios from §6, including dedup/cap ordering and mid-band ack confirmation.
- **Integration tests** — end-to-end with a real database, mocked coding_agent and vcs adapters.
- **E2E** — Playwright test for multi-review display and a single reply round-trip.

Eval suites live under `domain/reviewer/eval/` (singular), one `.eval.py` per prompt + fixtures.

## 13. Implementation order

1. **Module-doc template update** (§17) — small system-instructions PR before any new module docs are written so they conform.
2. **`coding_agent` task modes** — extend `coding_agent` with `incremental_review`, `verify_fix`, `stale_check` task types and their structured output schemas. Each ships with its eval fixtures under `domain/reviewer/eval/`.
3. **`core/llm` module** (§14) — pre-req for reply classification.
4. Schema migration + new tables; rip out JSONB findings; rename `review_jobs` → `reviews`; add `sequence_number` and `pending_replay`.
5. `lock.py` advisory-lock helper + `AggregateRepository` Protocol + SQLAlchemy and in-memory implementations.
6. Aggregate + state machine + fingerprint + anchor (pure code, full unit coverage).
7. Wire `schedule_review` through the aggregate; verify initial-review flow works (§6.1).
8. Trigger policy + debounce + `pending_replay`; wire `handle_push` to auto-incremental (§6.2).
9. Reply classifier (first caller of `core/llm`) + its eval fixtures; wire `handle_developer_reply` including mid-band confirmation flow (§6.4).
10. Verify-fix flow wired through coding_agent `verify_fix` mode (§6.5).
11. Stale-check flow wired through coding_agent `stale_check` mode.
12. UI changes (multi-review render + threads + All Conversations).
13. Manual full-review trigger from UI + PR comment (`@yaaos full review`).
14. Module doc (`apps/backend/docs/domain_reviewer.md`) — terse version derived from this plan, following the §17 template.

## 14. `core/llm` module plan

Supporting module. Provides mechanics for direct, single-shot, structured LLM calls. Reviewer's `classify_reply` is the first caller.

### 14.1 Purpose

Mechanics for **text-only**, single-shot, structured LLM calls with prompts loaded from files. Code-touching LLM work goes through `coding_agent`, not here.

Concerns owned: prompt-file loading, jinja2 templating, LangChain runnable construction, structured output validation, retries, gateway routing. Prompts and schemas live in the owner domain module.

### 14.2 Public interface

Exports from `__init__.py`:

- `FilePrompt` — value object representing one parsed prompt file (frontmatter metadata + jinja2-template body).
- `PromptRunnable` — LangChain `Runnable`. Constructed with a `FilePrompt` + a Pydantic output schema. Exposes `async ainvoke(input_vars) -> ParsedOutputT`.
- `load_prompt(path: Path) -> FilePrompt` — explicit path. No stack-inspection magic.

### 14.3 Prompt file format

One file per prompt. Extension `.prompt.md` — frontmatter is standard in markdown ecosystems; jinja2 syntax renders as plain text in previews; universal editor support.

YAML frontmatter required: `name`, `version`, `model`. Other fields passed to `init_chat_model` as model params.

Body is a jinja2 template, split into messages by `<system>`, `<user>`, `<assistant>` markers. Whitespace trimmed. Rendered by jinja2 *before* LangChain sees the messages — we do not mix LangChain's own templating.

```
---
name: classify_reply
version: 1
model: anthropic:claude-3-5-haiku-latest
temperature: 0.1
max_tokens: 1024
---
<system>
You are a classifier for developer replies on code review findings.
...
</system>
<user>
Finding: {{ finding.title }}
{{ finding.body }}

Developer reply:
{{ reply }}
</user>
```

Optional `<messages_placeholder name="history"/>` marker for cases that need spliced prior turns. Not used for POC.

### 14.4 `FilePrompt` (value object)

Fields:
- `name: str`, `version: int`
- `model: str` (LangChain `init_chat_model` spec, e.g. `anthropic:claude-haiku-4-5`)
- `model_params: dict` (temperature, max_tokens, etc.)
- `messages: list[ParsedMessage]` — each carries role + raw jinja2 template; rendered at invoke time.
- `source_path: Path` — for debugging.

Methods:
- `render(input_vars: dict) -> list[BaseMessage]` — renders all templates with the input, returns LangChain messages.

Immutable. No I/O after construction.

### 14.5 `PromptRunnable` behavior

On every `ainvoke`:

1. Render messages from `FilePrompt` + input vars.
2. Build chat model via `init_chat_model(model=..., **model_params)` with structured output bound to the Pydantic schema (`with_structured_output(schema, include_raw=True)`).
3. Set the OpenAI `user` parameter (or Anthropic `metadata.user_id`) to `f"{prompt_name}.v{version}"`. The gateway logs this with the request, giving per-prompt grouping in Braintrust **without breaking model routing** and without any header injection or span wrapper.
4. Invoke.
5. Validate output. On malformed parse: one retry with the same input. Then raise `MalformedOutput`.
6. Return the parsed Pydantic instance. Expose `usage` and `raw` as attributes for callers that want to log.

No span wrapping, no header injection, no LangChain callback handlers. Observability comes entirely from the gateway logs.

### 14.6 Gateway routing (`gateway.py`)

Exposes a single function: `configure_gateway()`. Called **explicitly from app startup**, not as a module-import side effect (avoids test contamination and load-order surprises).

Reads settings via `core/configuration`:

- `BRAINTRUST_API_KEY` — gateway auth (already in env).
- `BRAINTRUST_API_URL` — gateway base URL (Braintrust's proxy endpoint).

When configured, sets `OPENAI_API_BASE` / `ANTHROPIC_API_BASE` and the matching keys (both point at the Braintrust gateway with `BRAINTRUST_API_KEY` as the bearer credential) in the process environment so `init_chat_model` picks them up automatically. No per-call config.

If gateway settings are missing → no-op; LangChain falls back to direct provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Useful for local dev and tests.

### 14.7 Test caching

`core/llm` test harness wires `langchain.cache.SQLiteCache` pointed at `apps/backend/test/.llm-cache.sqlite` (gitignored). Effect: first run hits the gateway, subsequent runs replay from the cache. Cache key includes prompt content + model + params, so changes invalidate naturally.

Tests that exercise `PromptRunnable` directly may also mock `init_chat_model` for fully offline runs. Eval suites do NOT use the cache — evals must hit the model fresh each run.

### 14.8 Offline evals

Live in the owner module under `domain/<module>/eval/` (singular, matching `test/`). One eval file per prompt. Fixtures alongside.

```
domain/reviewer/
├── llm/
│   └── prompts/classify_reply.prompt.md
├── test/
└── eval/
    ├── classify_reply.eval.py     # uses braintrust.Eval()
    └── fixtures/classify_reply.jsonl
```

Owner imports `braintrust.Eval()` directly. `core/llm` does not own eval infrastructure. Add a thin wrapper only if two callers need the same scaffolding.

### 14.9 Internal layout

```
apps/backend/app/core/llm/
├── __init__.py
├── file_prompt.py
├── prompt_runnable.py
├── gateway.py
├── exceptions.py
└── test/
    ├── test_file_prompt.py
    ├── test_prompt_runnable.py
    └── fixtures/
        └── example.prompt.md
```

Roughly 150–200 LOC. No `braintrust` SDK dependency.

### 14.10 Explicitly NOT in `core/llm`

- Spans, traces, instrumentation, callback handlers.
- Per-call metadata injection (deferred; see §15 decisions).
- Prompt content (lives in owner modules).
- Agent loops, tool calling — coding_agent's job.
- RAG / vector retrieval.
- Cost budgeting, rate limiting.
- Remote eval runners.
- Prompt versioning logic (the file is the version).

### 14.11 Tach / module imports

- `core/llm` imports: `langchain`, `pydantic`, `jinja2`, `core/configuration` (for gateway settings).
- `core/llm` may be imported by any domain module.
- No domain imports inside `core/llm`.
- Run `apps/backend/bin/sync_modules` after the module lands.

### 14.12 Internal implementation order

1. `FilePrompt` + parser (frontmatter, body splitter, jinja2 render). Pure code; full unit tests, no langchain needed.
2. `gateway.configure_gateway()` + env-patching unit tests.
3. `PromptRunnable` wrapping `init_chat_model` + structured output + `user` field tagging. Tests mock `init_chat_model`.
4. First caller migration: reviewer's `classify_reply` (with its eval fixtures landing alongside).
5. Module doc following the updated template (§17).

## 15. Decisions made

- **`review_jobs` → `reviews` rename**: done in the same PR as the schema migration. One disruption, not two.
- **Classification confidence threshold**: hardcoded for POC (see §10.3).
- **`acknowledgment` reply**: yes, post a short reply ("noted, will skip this in future reviews").
- **`verify_fix` GitHub thread resolution**: do NOT auto-resolve threads. Post a "confirmed fixed" reply and leave thread state to the human. Reply is the signal.
- **`respond_to_question` context scope**: local code only for POC (the finding's anchor + nearby code). Moot for POC — call deferred.
- **`AckKind` for POC**: two values — `intentional`, `wontfix`. (Resolution from review feedback.)
- **`comment_threads.status`**: column dropped. Finding state already conveys lifecycle.
- **Linter / typecheck pre-pass (§10.7)**: deferred. POC relies on the "do NOT flag" prompt list.
- **Concurrency**: PG advisory lock per `pr_id` on every mutating entry point (§5.7).
- **`superseded` finding state**: dropped from POC — no clear implementation path.
- **POC reply classifier output**: 3-class — `acknowledgment | verify_fix | other`.
- **Anchor and fingerprint hash definitions**: concrete; specified in §2.3.
- **`handle_developer_reply` signature**: takes GitHub external IDs; intake resolves to internal records.
- **Gateway configuration**: explicit `configure_gateway()` called at app startup, not module-import side effect.
- **`Review.sequence_number`**: per-PR ordinal column, used for UI labels.
- **Severity is sticky; confidence is `max(stored, new)` across re-observations.**
- **`pushback` reply handling (when intent is reactivated post-POC)**: agent replies once with sharper rationale, then stops. No further argument loop.
- **`correction` reply handling (when intent is reactivated post-POC)**: same confidence threshold as `verify_fix` (≥ 0.80) to withdraw the original finding.
- **All Conversations data**: backend stores `resolved_confirmed` findings durably and includes them in the underlying data set, but the API does not return them for the All Conversations view in POC (UI shows only open / acknowledged threads). The storage choice keeps the option open to surface them later without a backfill.
- **Classifier model**: cheapest current Anthropic Haiku (older generation). Pinned in the prompt frontmatter, not hardcoded in code, so it can move forward without a code change.
- **LangChain LLM caching in tests**: unit tests use `langchain.cache.InMemoryCache` (or `SQLiteCache` on disk for cross-run reuse) so re-runs don't hit the network. Wired in the test harness; production code is unchanged.

## 16. Open questions

(None. Prior items resolved — see §15.)

## 17. Module-doc template update (system-instructions change)

Module docs currently follow the template: **Purpose · Public interface · Module architecture · Data owned · How it's tested**. The **Module architecture** section has no required internal structure today. As part of this work, update the project's module-doc conventions so the Module architecture section must contain — in this order:

- **Entities** — DDD entities owned by this module. One bullet per entity, one sentence: what it represents and what gives it identity.
- **Key value objects** — only the load-bearing ones; not every tiny dataclass. One bullet, one sentence each.
- **Core user flows** — short numbered steps for the main ways callers exercise this module. Pure prose; no code.
- **State machines** — if the module has any, list states and transitions. One state per bullet, transitions as a small table or arrow notation.

Discipline still applies (terse, bullets, no code snippets, no `Decisions` section, link don't repeat).

Files to update when this change lands (both confirmed to exist):
- `apps/backend/docs/patterns.md` — backend module-doc conventions.
- `apps/web/docs/patterns.md` — frontend equivalent.
- Repo-root `CLAUDE.md` — if the doc-discipline rules there need to mirror the addition.

Retroactive application: update existing module docs only as we touch them. Not a sweep.
