# domain/pr_review

> Inbound free-text PR comment classification + batching into comment-response pipeline runs.

## Purpose

Owns the `pr_comments` table — every inbound PR comment yaaos tracks, from `@yaaos` grammar handling through classification and batching into a single comment-response run per ticket. Lifecycle is derived, not a status column. Does not own posting or conversation policy — that's `plugins/github`'s `github:reply_to_comment` action, which reads this module's `list_comments_for_run`.

## Public interface

`PRComment` (full VO), `InboundComment` (VCS-agnostic wire input from the plugin), `handle_pr_comment` (the entry point), `maybe_start_batch_run`, `list_comments_for_run`, `evaluate_auto_approval` (stub — wiring lands separately). No HTTP routes — this module has no UI surface; it's driven by the VCS plugin's webhook handler.

## Module architecture

### Entities

- **PRComment** — one inbound free-text PR comment. `finding_id` is set when the comment replies to a finding thread (resolved via `findings.find_by_external_comment`); `classification` is `null` until classified; `claimed_by_run_id` is set once a batch run claims it.

### Key value objects

- **InboundComment** — the VCS-agnostic shape the plugin hands in (external id, author, body, in-reply-to).

### Core user flows

1. **`handle_pr_comment(org_id, ticket_id, comment, session)`** — the entry from `plugins/github`. The `@yaaos` grammar is handled inline first (`core/intake.parse_yaaos_command` returns `"re-review" | "cancel" | None`): `re-review` resolves the repo's `github:pr_opened` bindings and calls `pipelines.start_run` per binding (queues if busy); `cancel` resolves `tickets.current_run_id` and calls `pipelines.request_cancel` (a terminal-already run is a no-op). `request_cancel` reads its org id off the auth contextvar, so the webhook path — unauthenticated, no `ORG_SCOPED` middleware — opens `core.auth.org_context` explicitly around the call. Free text resolves a finding anchor via `findings.find_by_external_comment` (`None` when the comment isn't a reply, or the parent isn't a posted finding), inserts a `pr_comments` row, and enqueues `CLASSIFY_COMMENT`.
2. **`CLASSIFY_COMMENT`** (task) — idempotent on `classification` (redelivery of an already-classified comment is a no-op). No finding anchor stamps `unclear` outright (no LLM call). Otherwise runs `domain/pr_review/llm.classify_comment` (org BYOK Anthropic key via `core/byok`) and stamps `unclear` when confidence falls below threshold, else the LLM's own intent (`question` / `claims_fixed` / `dispute`). The classification stamp commits in its own transaction *before* any reply is attempted (a crash between the two loses one canned reply on redelivery rather than risking a double reply, since redelivery sees `classification` already set and returns early). `unclear` posts a canned clarification reply via `vcs.post_comment_reply`; anything else hands off to `maybe_start_batch_run`.
3. **`maybe_start_batch_run(org_id, ticket_id, session)`** — no-op if a run is already in flight (`pipelines.has_run_in_flight`) or no comment is waiting (classified, unclaimed, non-`unclear`). Otherwise resolves the repo's `github:pr_comment` binding, renders the waiting batch into one `Kickoff.input_text`, starts a run, and stamps `claimed_by_run_id` on every comment in the batch — atomically with the same query that selected them (`SELECT ... FOR UPDATE`), so a comment classified mid-run genuinely waits for the next batch rather than racing into this one.
4. **`AFTER_RUN_TERMINAL`** (task) — registered with `domain/pipelines.register_run_terminal_hook` at import time; every pipeline run reaching a terminal state re-invokes `maybe_start_batch_run` for that ticket, so comments that arrived mid-run get their own batch once the ticket is free again. (Auto-approval re-evaluation is not wired into this hook yet — `evaluate_auto_approval` stays a stub.)
5. **`list_comments_for_run(run_id, session)`** — every `pr_comments` row claimed by a run; consumed by `github:reply_to_comment`, which cross-references `classification == "dispute"` against the run's verdicts to apply the defend/dismiss policy.

The unified `prior_findings` rule the engine feeds every review pass includes, for a comment-response run, the findings referenced by its own claimed batch — regardless of the finding's own status — via a registered callback (`domain/pipelines.register_comment_findings_provider`); a disputed finding may already be resolved or dismissed by the time the dispute lands, and the skill still needs to see it to answer.

### State machines

Comment lifecycle is derived from column state, not a status enum: `NULL classification` = awaiting classify · `unclear` = terminal (canned reply, never batched) · classified + unclaimed = waiting · claimed = in a run.

## Data owned

- `pr_comments` — one row per inbound comment. `UNIQUE(org_id, comment_external_id)`. `CHECK` constraint on `classification`. `finding_id` FK → `pipeline_findings(id)` (owned by `domain/findings`).

## How it's tested

- `domain/pr_review/test/test_comment_loop_service.py` — Acceptance: `@yaaos re-review` starts a run; `@yaaos cancel` cancels the ticket's current run; a `question` reply is classified, batched, and its reply lands on the finding's thread on a live `apps/fake-github`; a `claims_fixed`/`dispute` pair arriving mid-run waits and joins the next batch once the first run terminates; an unanchored comment is always `unclear` and never joins a batch.
- `domain/pr_review/test/test_defense_policy_service.py` — the two-round dispute policy (defend once, then a second dispute forces a deterministic dismiss) against `app.testing.stub_vcs`.
