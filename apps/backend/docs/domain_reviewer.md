# domain/reviewer

> Review-workflow orchestrator + durable findings. The workflow engine drives every review run; `publish_findings` converts skill output into canonical `Finding` rows.

## Scope

Owns review runs and the findings they produce: `Review`s and `Finding`s. Findings carry the canonical schema — `severity ∈ {blocker, should_fix, nit}`, `confidence ∈ {verified, plausible, speculative}`, `category`, `rationale`, `rule_violated`, `rule_source`, `suggested_fix`, optional `file`/`line`, persisted `finding_display_id`.

Also owns the skill-output contract types: `ReviewContext` (the remote dispatch context), `ReportedFindingShape` (the strict-enum, per-finding shape validated by the engine before any DB write), and `CodeReviewResponse` (the expected JSON response shape for the `pr_review` skill — declared as `CodeReview.ExpectedResponse`, auto-injected as `output_schema` in the skill prompt by `CodingAgentCommand.@final dispatch`).

Does NOT call an LLM for code review — `core/coding_agent` + `plugins/claude_code` do that. Reviewer is skill-agnostic: it dispatches the review and writes whatever findings the skill emits, validating against the canonical schema.

## Workflows + commands

One workflow in `domain/reviewer/workflows/`, plus reused workspace lifecycle commands from [`core/workspace`](core_workspace.md):

- `pr_review_v1` — `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`, `finalizer=cleanup`. All step data flows through typed `Inputs`/`Outputs` Pydantic models; the workflow declares a `TicketSnapshot` as its `workflow_input`, and each step's `inputs_factory` lambda reads fields from prior steps' `StepRef.outputs`.

`CheckShouldReview` reads `is_draft`, `is_fork`, `labels`, `author_login` from its typed `CheckShouldReviewInputs` and returns `skip` on draft, fork, `yaaos-skip`/`no-review`/`wip` labels (case-insensitive), or `*[bot]`/`*-bot` author. `CleanupWorkspace` runs as the workflow's `finalizer` `StepRef` on any terminal-fail, exactly once.

`SecretsScan` runs a deterministic regex sweep (`secrets_detection.detect_secrets`) over `+`-prefixed diff lines for AWS access keys, GitHub tokens, Anthropic/OpenAI keys, and PEM private keys; on a match it posts a refusal comment via `vcs.post_comment` and returns `skip`. AWS-published example keys (`AKIAIOSFODNN7EXAMPLE`, `AKIAI44QH8DHBEXAMPLE`) are allowlisted so PRs shipping AWS doc snippets or fixtures aren't refused.

## Core flow

For the top-level review arc see [`docs/system-architecture.md`](../../../docs/system-architecture.md). Reviewer-internal detail only:

`CodeReview` dispatches the coding-agent invocation against the provisioned workspace via `coding_agent.dispatch_invocation`. On `completed_success`, the engine calls `CodingAgentCommand.handle_response(output, ctx)` on the `CodeReview` command instance: validates the agent's JSON output against `CodeReviewResponse.model_validate_json`, and on success returns `Outcome.success(outputs=CodeReviewOutputs(response=<parsed>))`. The workflow lambda for `PostFindings` reads `review.outputs.response.findings` — a typed `list[ReportedFindingShape]` requiring no further parsing. `PostFindings` calls `publish_findings` directly. **Non-conforming agent output → `handle_response` returns `Outcome.failure(retryable=False)` → FAIL_WORKFLOW without retry** — the schema validation gate; no findings are persisted or posted.

## `publish_findings` — the canonical entry point

`publish_findings(*, pr_id, org_id, pr_external_id, vcs_plugin_id, findings: list[ReportedFindingShape], session)` lives in [`publish.py`](../app/domain/reviewer/publish.py). Receives pre-validated `ReportedFindingShape` objects — `severity`/`confidence` are already strict Literal types (validated upstream by `handle_response`). The link from a review to its coding-agent activity is implicit through the shared `(workflow_execution_id, step_id)` keys on `coding_agent_runs`.

1. Open a `Review` row for this run.
2. Assign each finding a `finding_display_id` — per-`pr_id` `max+1`, monotonic across categories. Rendered as `<category-prefix>-<id>` (`sec-3`). The category→prefix map is the single hardcoded dict at the top of `publish.py`; unknown category slugifies to a lowercase alnum string ≤8 chars.
3. Persist each `Finding` row.
4. Post each finding to the VCS plugin via `vcs.post_finding` with named primitive args — no value object crosses the `vcs` boundary.

The skill never emits `finding_display_id`; yaaos assigns + persists it.

## Canonical output schema

`CodeReviewResponse.model_json_schema()` is the single source of truth — auto-injected by `CodingAgentCommand.@final dispatch` into `Invocation.context["output_schema"]` so the skill prompt carries the exact validated contract. No helper function needed. `ReportedFindingShape` in `domain/reviewer/types.py` is the strict-enum per-finding shape; its field set is pinned to the schema by a unit test.

## Invariants + why

- **Skill owns all filtering.** No admission pipeline, no per-severity threshold, no per-PR nit cap, no fingerprint dedup. The skill decides what to surface; yaaos validates the schema and posts the result.
- **Schema gate is authoritative + runtime.** Non-conforming agent output fails the `handle_response` parse cleanly (returns `Outcome.failure(retryable=False)`) — no findings persisted, no findings posted, workflow ends in `failed` without consuming the retry budget.
- **Advisory lock first.** `lock.acquire_pr_lock` issues `pg_advisory_xact_lock(hashtext('pr:<uuid>')::bigint)` inside the transaction before any reviewer write. Two concurrent webhooks for the same PR serialize; lock releases on commit/rollback. Read-only paths do NOT take the lock. The lock serializes both per-PR sequence-number monotonicity AND the at-most-one-in-flight-review invariant: `_create_incremental_review` re-checks the in-flight predicate inside the lock, so the loser of a two-push race stamps `pending_replay=True` on the winner's row and returns `skipped:in_flight` rather than launching a second review.
- **`(pr_id, finding_display_id)` is unique.** Enforced at the table level; the assignment in `publish_findings` reads `max+1` and assigns inside the caller's transaction.
- **`dispatch_events` and `dispatch_audits` run BEFORE `session.commit()`.** Domain events stash for post-commit SPA fan-out; audit rows are written in the same transaction as the state change. Rolled-back transactions silently discard both stashes — no phantom SPA events, no orphan audits.

## Data owned

- `reviews` — one row per PR run. `sequence_number` (per-PR ordinal), `trigger_reason`, `commit_sha_at_start`, `scope_prev_sha`. Run config: `model`, `effort`. Lifecycle state: `current_step`, `last_heartbeat_at`, `completed_at`, `skip_reason`, `error_message`. `pending_replay` is write-only — stamped True when a push arrives while a review is in-flight on the same PR; no production reader (replay-on-completion is separate work). The link from a review to its coding-agent activity is implicit through the shared `(workflow_execution_id, step_id)` keys on `coding_agent_runs`.
- `findings` — canonical schema: `severity, confidence, category, rationale, rule_violated, rule_source, suggested_fix, file (nullable), line (nullable), review_id (FK → reviews.id), finding_display_id`. Unique `(pr_id, finding_display_id)`.

## Vocabulary

- `ReviewContext` — remote dispatch context; its fields are serialised into `Invocation.context` by `CodeReview.build_invocation`. Fields: `org_id`, `repo_external_id`, `pr_external_id`, `head_sha`, `base_sha`. Lives in `domain/reviewer`.
- `ReportedFindingShape` — the strict-enum per-finding type: `severity ∈ {blocker, should_fix, nit}`, `confidence ∈ {verified, plausible, speculative}`, and all text fields. Frozen + `extra="forbid"`. Validated by `CodingAgentCommand.handle_response` via `CodeReviewResponse.model_validate_json`. Also the type `PostFindings` receives from the workflow dataflow. Lives in `domain/reviewer`.
- `CodeReviewResponse` — expected JSON response shape from the `pr_review` skill; declared as `CodeReview.ExpectedResponse`. `findings: list[ReportedFindingShape]`. Auto-injected as `output_schema` in the invocation context by `@final dispatch`. Lives in `domain/reviewer`.
- `Finding` — validated, persisted finding. Lives in `domain/reviewer`.
- `finding_display_id` — per-PR monotonic integer; rendered as `<category-prefix>-<id>` (`sec-3`, `arch-7`).
- `Review` — one row per PR review run.

## Ticket status — atomic flips on workflow bootstrap and terminal

`pr_review_v1` declares `on_start=transition_ticket_on_start` and `on_terminal=transition_ticket_on_terminal` (from [`domain/tickets/workflow_callbacks.py`](domain_tickets.md)) directly on the `Workflow` dataclass. No global start-hook or terminal-hook registry; no startup registration call needed.

**On `pr_review_v1` bootstrap** (`route_workflow` first transitions to RUNNING) the engine awaits `on_start`, which calls [`tickets.transition_on_workflow_start`](domain_tickets.md) inside the bootstrap-commit transaction:

- `pending → running` (atomically with the workflow RUNNING write)

Guard misses (ticket not found, ownership mismatch, ticket not in `pending`) return `False` silently — callback never raises.

**On every `pr_review_v1` terminal transition** the engine awaits `on_terminal`, which calls [`tickets.transition_on_workflow_terminal`](domain_tickets.md) inside the terminal-commit transaction:

- `DONE → ticket "done"` (reason omitted)
- `FAILED → ticket "failed"` (failure_reason threaded into the `ticket.status_changed` audit payload)
- `CANCELLED → ticket "cancelled"` (reason omitted)

Guard misses (ticket not found, ownership mismatch, already terminal) return silently — the callback never raises, so a guard miss never rolls back the workflow terminal write.

The orphan sweep (`orphan_sweep.py`) is a safety net only — it handles never-dispatched tickets that slipped through before a workflow started. It does NOT handle normal workflow termination; `on_terminal` covers that path atomically. Runs as a `@scheduled` worker task (`ticket_orphan_sweep`, cron `* * * * *`) — exactly one worker pod enqueues each minute slot.

## Findings rollup

After each review run (`PostFindings`), reviewer calls `refresh_ticket_findings_summary(ticket_id, pr_id, *, org_id, session)`. This recomputes `findings_count` + `max_severity` from the `findings` table and writes them to the ticket row via `tickets.update_findings_summary`. Tickets do not import reviewer — the dependency is one-way: reviewer → tickets.

`aggregate_findings_by_prs` is a reviewer-internal helper in `reviewer/service.py`; it is not part of the public module interface.

## How it's tested

- **Service tests** (`@pytest.mark.service`):
  - `test_start_hook_service.py` — `on_start` callback flips ticket pending→running; exactly one `ticket.status_changed` audit row (pending→running; `create_from_pr` writes `ticket.created`, not `ticket.status_changed`).
  - `test_terminal_hook_service.py` — 5 scenarios: DONE/FAILED/CANCELLED each flip the ticket; non-owning execution and workflow with no `on_terminal` are no-ops. **Coverage-scrutiny flag: primary gate for the atomic ticket-flip contract.**
  - `test_publish_findings_service.py` — strict-enum rejection at construction (`ReportedFindingShape` raises `ValidationError` for bad severity/confidence), `finding_display_id` per-`pr_id` monotonicity + uniqueness, `ReportedFindingShape`-vs-`CodeReviewResponse` schema pin, VCS `post_finding` transport.
  - `test_publish_findings_without_run_id_service.py` — `publish_findings` with the new signature (no `run_id` param) inserts a Review row; confirms `reviews.run_id` column is absent from the schema.
  - `test_post_findings_happy_path.py` — typed `ReportedFindingShape` list flows through `PostFindings` and persists with canonical schema.
  - `test_post_findings_reads_output_service.py` — `PostFindings` reads `inputs.findings` list; empty list with no pr_id → zero findings; typed list → finding persists.
  - `app/core/coding_agent/test/test_handle_response_service.py` — `handle_response` valid JSON → typed `Outcome.success`; schema violation → `Outcome.failure(retryable=False)`; empty string → `failure(retryable=False)`; schema auto-injected via `ExpectedResponse` ClassVar.
  - `test_code_review_dispatch_new_path_service.py` — `CodeReview.dispatch` via `coding_agent.dispatch_invocation`: asserts `agent_commands` row with `command_kind="InvokeClaudeCode"`, `coding_agent_runs` row with `plugin_id="claude_code"`, and workspace claim acquired.
  - `test_pr_review_v1_e2e_service.py` — full pipeline (stub VCS + coding-agent + workspace).
  - `test_findings_summary_service.py` — rollup written on review end.
  - `test_start_incremental_review_under_lock_service.py` — two concurrent pushes to the same PR race; exactly one ReviewRow + one `engine.start`, loser returns `skipped:in_flight`, surviving row carries `pending_replay=True`.
  - `test_secrets_scan_service.py`, `test_cancel_dual_write_service.py`, `test_reviewer_activity_publish_service.py`.
