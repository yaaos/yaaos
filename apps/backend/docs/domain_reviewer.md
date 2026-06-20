# domain/reviewer

> Review-workflow orchestrator + durable findings. The workflow engine drives every review run; `publish_findings` converts skill output into canonical `Finding` rows.

## Scope

Owns review runs and the findings they produce: `Review`s and `Finding`s. Findings carry the canonical schema — `severity ∈ {blocker, should_fix, nit}`, `confidence ∈ {verified, plausible, speculative}`, `category`, `rationale`, `rule_violated`, `rule_source`, `suggested_fix`, optional `file`/`line`, persisted `finding_display_id`.

Also owns the skill-output contract types: `ReviewContext` (the remote dispatch context), `ReportedFinding` (the raw, pre-validation skill output), `FindingDraftList` (the internal Pydantic model for stream-JSON parsing), `finding_output_schema()` (generates the JSON schema appended to the skill prompt and used to validate output), and `parse_review_output()` (finds the terminal `type=result` stream event, validates against `FindingDraftList`, returns `list[ReportedFinding]`, raises `ValueError` on any failure).

Does NOT call an LLM for code review — `core/coding_agent` + `plugins/claude_code` do that. Reviewer is skill-agnostic: it dispatches the review and writes whatever findings the skill emits, validating against the canonical schema.

## Workflows + commands

One workflow in `domain/reviewer/workflows/`, plus reused workspace lifecycle commands from [`core/workspace`](core_workspace.md):

- `pr_review_v1` — `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`, `finalizer=cleanup`. All step data flows through typed `Inputs`/`Outputs` Pydantic models; the workflow declares a `TicketSnapshot` as its `workflow_input`, and each step's `inputs_factory` lambda reads fields from prior steps' `StepRef.outputs`.

`CheckShouldReview` reads `is_draft`, `is_fork`, `labels`, `author_login` from its typed `CheckShouldReviewInputs` and returns `skip` on draft, fork, `yaaos-skip`/`no-review`/`wip` labels (case-insensitive), or `*[bot]`/`*-bot` author. `CleanupWorkspace` runs as the workflow's `finalizer` `StepRef` on any terminal-fail, exactly once.

## Core flow

For the top-level review arc see [`docs/system-architecture.md`](../../../docs/system-architecture.md). Reviewer-internal detail only:

`CodeReview` dispatches the coding-agent invocation against the provisioned workspace via `coding_agent.dispatch_invocation` (which enqueues the `InvokeClaudeCode` AgentCommand, inserts a `coding_agent_runs` row, and pins the command to the owning agent). The agent runs the assigned skill and returns its output; the run-sink processes the terminal event, calls `plugin.parse_result`, and contributes `{"output": ...}` to the step's `Outputs` (via `CodeReviewOutputs.output`). `PostFindings` receives `output` via typed `PostFindingsInputs`, parses it into `list[ReportedFinding]` via `parse_review_output`, and hands them to `publish_findings`. **Non-conforming agent output (parse failure or out-of-range enum) → `Outcome.failure(reason="schema_invalid") → FAIL_WORKFLOW`** — the runtime gate; no findings are persisted or posted.

## `publish_findings` — the canonical entry point

`publish_findings(*, pr_id, org_id, pr_external_id, vcs_plugin_id, findings: list[ReportedFinding], session)` lives in [`publish.py`](../app/domain/reviewer/publish.py). The link from a review to its coding-agent activity is implicit through the shared `(workflow_execution_id, step_id)` keys on `coding_agent_runs`.

1. Open a `Review` row for this run.
2. For each `ReportedFinding`: validate `severity`/`confidence` raw strings against the `Severity`/`Confidence` `Literal` aliases — out-of-range raises (caught by `PostFindings` as the runtime gate above).
3. Assign each finding a `finding_display_id` — per-`pr_id` `max+1`, monotonic across categories. Rendered as `<category-prefix>-<id>` (`sec-3`). The category→prefix map is the single hardcoded dict at the top of `publish.py`; unknown category slugifies to a lowercase alnum string ≤8 chars.
4. Persist each `Finding` row.
5. Post each finding to the VCS plugin via `vcs.post_finding` with named primitive args — no value object crosses the `vcs` boundary.

The skill never emits `finding_display_id`; yaaos assigns + persists it.

## Canonical output schema

`finding_output_schema() -> dict` (in `domain/reviewer.__all__`) is the single source of truth — generated from a Pydantic model's `model_json_schema()`. The skill-invocation prompt appends this schema as a strict output contract; `PostFindings` calls `parse_review_output` directly (no plugin lookup) to validate and parse the agent's stream-json stdout. `ReportedFinding` in `domain/reviewer/types.py` is the lenient raw-string parse twin; a unit test pins its field set to `finding_output_schema()`.

## Invariants + why

- **Skill owns all filtering.** No admission pipeline, no per-severity threshold, no per-PR nit cap, no fingerprint dedup. The skill decides what to surface; yaaos validates the schema and posts the result.
- **Schema gate is authoritative + runtime.** Out-of-range severity/confidence fails the run cleanly — no findings persisted, no findings posted, workflow ends in `failed` with `failure_reason="schema_invalid"`.
- **Advisory lock first.** `lock.acquire_pr_lock` issues `pg_advisory_xact_lock(hashtext('pr:<uuid>')::bigint)` inside the transaction before any reviewer write. Two concurrent webhooks for the same PR serialize; lock releases on commit/rollback. Read-only paths do NOT take the lock. The lock serializes both per-PR sequence-number monotonicity AND the at-most-one-in-flight-review invariant: `_create_incremental_review` re-checks the in-flight predicate inside the lock, so the loser of a two-push race stamps `pending_replay=True` on the winner's row and returns `skipped:in_flight` rather than launching a second review.
- **`(pr_id, finding_display_id)` is unique.** Enforced at the table level; the assignment in `publish_findings` reads `max+1` and assigns inside the caller's transaction.
- **`dispatch_events` and `dispatch_audits` run BEFORE `session.commit()`.** Domain events stash for post-commit SPA fan-out; audit rows are written in the same transaction as the state change. Rolled-back transactions silently discard both stashes — no phantom SPA events, no orphan audits.

## Data owned

- `reviews` — one row per PR run. `sequence_number` (per-PR ordinal), `trigger_reason`, `commit_sha_at_start`, `scope_prev_sha`. Run config: `model`, `effort`. Lifecycle state: `current_step`, `last_heartbeat_at`, `completed_at`, `skip_reason`, `error_message`. `pending_replay` is write-only — stamped True when a push arrives while a review is in-flight on the same PR; no production reader (replay-on-completion is separate work). The link from a review to its coding-agent activity is implicit through the shared `(workflow_execution_id, step_id)` keys on `coding_agent_runs`.
- `findings` — canonical schema: `severity, confidence, category, rationale, rule_violated, rule_source, suggested_fix, file (nullable), line (nullable), review_id (FK → reviews.id), finding_display_id`. Unique `(pr_id, finding_display_id)`.

## Vocabulary

- `ReviewContext` — remote dispatch context; its fields are serialised into `Invocation.context` by `CodeReview.dispatch` before calling `coding_agent.dispatch_invocation`. Fields: `org_id`, `repo_external_id`, `pr_external_id`, `head_sha`, `base_sha`. Lives in `domain/reviewer`.
- `ReportedFinding` — raw skill output before schema validation; raw strings, no enums. Lives in `domain/reviewer`.
- `FindingDraftList` — internal Pydantic model wrapping a `list[ReportedFinding]`; used by `parse_review_output` to validate the stream-json payload. Lives in `domain/reviewer`.
- `finding_output_schema()` — returns the JSON schema for the skill's `findings` output; injected into the prompt appendix by `core/coding_agent/prompts.py`. Lives in `domain/reviewer`.
- `parse_review_output(stdout)` — parses stream-json stdout into `list[ReportedFinding]`; raises `ValueError` on any parse failure. Lives in `domain/reviewer`.
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
  - `test_publish_findings_service.py` — enum gate (rejects out-of-range `severity`/`confidence`), `finding_display_id` per-`pr_id` monotonicity + uniqueness, `ReportedFinding`-vs-`finding_output_schema()` schema pin.
  - `test_publish_findings_without_run_id_service.py` — `publish_findings` with the new signature (no `run_id` param) inserts a Review row; confirms `reviews.run_id` column is absent from the schema.
  - `test_parse_review_output_owns_service.py` — unit tests for `domain/reviewer.parse_review_output`: valid stdout → `list[ReportedFinding]`, null-anchor accepted, no-result-event raises, empty stdout raises, invalid JSON raises, wrong schema raises, last result event wins.
  - `test_post_findings_happy_path.py` — `ReportedFinding`s flow through `PostFindings` end-to-end and persist with canonical schema (via `inputs["output"]` key).
  - `test_post_findings_reads_output_service.py` — `PostFindings` reads `inputs["output"]` (not `stdout`); empty/missing key → zero findings; valid stdout → finding persists without `run_id`.
  - `test_code_review_dispatch_new_path_service.py` — `CodeReview.dispatch` via `coding_agent.dispatch_invocation`: asserts `agent_commands` row with `command_kind="InvokeClaudeCode"`, `coding_agent_runs` row with `plugin_id="claude_code"`, and workspace claim acquired.
  - `test_pr_review_v1_e2e_service.py` — full pipeline (stub VCS + coding-agent + workspace).
  - `test_findings_summary_service.py` — rollup written on review end.
  - `test_start_incremental_review_under_lock_service.py` — two concurrent pushes to the same PR race; exactly one ReviewRow + one `engine.start`, loser returns `skipped:in_flight`, surviving row carries `pending_replay=True`.
  - `test_secrets_scan_service.py`, `test_cancel_dual_write_service.py`, `test_reviewer_activity_publish_service.py`.
