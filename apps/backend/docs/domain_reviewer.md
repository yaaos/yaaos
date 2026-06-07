# domain/reviewer

> Review-workflow orchestrator + durable findings. The workflow engine drives every review run; `publish_findings` converts skill output into canonical `Finding` rows.

## Scope

Owns review runs and the findings they produce: `Review`s and `Finding`s. Findings carry the canonical schema — `severity ∈ {blocker, should_fix, nit}`, `confidence ∈ {verified, plausible, speculative}`, `category`, `rationale`, `rule_violated`, `rule_source`, `suggested_fix`, optional `file`/`line`, persisted `finding_display_id`.

Does NOT call an LLM for code review — `domain/coding_agent` + `plugins/claude_code` do that. Reviewer is skill-agnostic: it dispatches the review and writes whatever findings the skill emits, validating against the canonical schema.

## Workflows + commands

One workflow in `domain/reviewer/workflows/`, plus reused workspace lifecycle commands from [`core/workspace`](core_workspace.md):

- `pr_review_v1` — `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`, `finalizer_step_id="cleanup"`.

`CheckShouldReview` returns `skip` on draft, fork, `yaaos-skip`/`no-review`/`wip` labels (case-insensitive), `*[bot]`/`*-bot` author. `CleanupWorkspace` runs as the workflow's `finalizer` step on any terminal-fail, exactly once.

## Core flow

For the top-level review arc see [`docs/system-architecture.md`](../../../docs/system-architecture.md). Reviewer-internal detail only:

`CodeReview` dispatches the coding-agent invocation against the provisioned workspace; the agent runs the assigned skill against the real clone and returns its output. `PostFindings` parses the agent's output into `list[ReportedFinding]` and hands them to `publish_findings`. **Non-conforming agent output (parse failure or out-of-range enum) → `Outcome.failure(reason="schema_invalid") → FAIL_WORKFLOW`** — the runtime gate; no findings are persisted or posted.

## `publish_findings` — the canonical entry point

`publish_findings(*, pr_id, org_id, pr_external_id, vcs_plugin_id, findings: list[ReportedFinding], session)` lives in [`publish.py`](../app/domain/reviewer/publish.py):

1. Open a `Review` row for this run.
2. For each `ReportedFinding`: validate `severity`/`confidence` raw strings against the `Severity`/`Confidence` `Literal` aliases — out-of-range raises (caught by `PostFindings` as the runtime gate above).
3. Assign each finding a `finding_display_id` — per-`pr_id` `max+1`, monotonic across categories. Rendered as `<category-prefix>-<id>` (`sec-3`). The category→prefix map is the single hardcoded dict at the top of `publish.py`; unknown category slugifies to a lowercase alnum string ≤8 chars.
4. Persist each `Finding` row.
5. Post each finding to the VCS plugin via `vcs.post_finding` with named primitive args — no value object crosses the `vcs` boundary.

The skill never emits `finding_display_id`; yaaos assigns + persists it.

## Canonical output schema

`finding_output_schema() -> dict` (in `domain/coding_agent.__all__`) is the single source of truth — generated from a Pydantic model's `model_json_schema()`. The skill-invocation prompt appends this schema as a strict output contract; `PostFindings` re-validates the returned findings against it. `ReportedFinding` in `domain/coding_agent/types.py` is the lenient raw-string parse twin; a unit test pins its field set to `finding_output_schema()`.

## Invariants + why

- **Skill owns all filtering.** No admission pipeline, no per-severity threshold, no per-PR nit cap, no fingerprint dedup. The skill decides what to surface; yaaos validates the schema and posts the result.
- **Schema gate is authoritative + runtime.** Out-of-range severity/confidence fails the run cleanly — no findings persisted, no findings posted, workflow ends in `failed` with `failure_reason="schema_invalid"`.
- **Advisory lock first.** `lock.acquire_pr_lock` issues `pg_advisory_xact_lock(hashtext('pr:<uuid>')::bigint)` inside the transaction before any reviewer write. Two concurrent webhooks for the same PR serialize; lock releases on commit/rollback. Read-only paths do NOT take the lock.
- **`(pr_id, finding_display_id)` is unique.** Enforced at the table level; the assignment in `publish_findings` reads `max+1` and assigns inside the caller's transaction.
- **`dispatch_events` and `dispatch_audits` run BEFORE `session.commit()`.** Domain events stash for post-commit SPA fan-out; audit rows are written in the same transaction as the state change. Rolled-back transactions silently discard both stashes — no phantom SPA events, no orphan audits.

## Data owned

- `reviews` — one row per PR run. `sequence_number` (per-PR ordinal), `trigger_reason`, `commit_sha_at_start`. Run-metric columns live on the central coding-agent-run tables.
- `findings` — canonical schema: `severity, confidence, category, rationale, rule_violated, rule_source, suggested_fix, file (nullable), line (nullable), review_id (FK → reviews.id), finding_display_id`. Unique `(pr_id, finding_display_id)`.

## Vocabulary

- `ReportedFinding` — raw skill output before schema validation; raw strings, no enums. Lives in `domain/coding_agent` (the agent's output type).
- `Finding` — validated, persisted finding. Lives in `domain/reviewer`.
- `finding_display_id` — per-PR monotonic integer; rendered as `<category-prefix>-<id>` (`sec-3`, `arch-7`).
- `Review` — one row per PR review run.

## Findings rollup

After each review run (`PostFindings`), reviewer calls `refresh_ticket_findings_summary(ticket_id, pr_id, *, org_id, session)`. This recomputes `findings_count` + `max_severity` from the `findings` table and writes them to the ticket row via `tickets.update_findings_summary`. Tickets do not import reviewer — the dependency is one-way: reviewer → tickets.

`aggregate_findings_by_prs` is a reviewer-internal helper in `reviewer/service.py`; it is not part of the public module interface.

## How it's tested

- **Service tests** (`@pytest.mark.service`):
  - `test_publish_findings_service.py` — enum gate (rejects out-of-range `severity`/`confidence`), `finding_display_id` per-`pr_id` monotonicity + uniqueness, `ReportedFinding`-vs-`finding_output_schema()` schema pin.
  - `test_post_findings_happy_path.py` — `ReportedFinding`s flow through `PostFindings` end-to-end and persist with canonical schema.
  - `test_pr_review_v1_e2e_service.py` — full pipeline (stub VCS + coding-agent + workspace).
  - `test_findings_summary_service.py` — rollup written on review end.
  - `test_secrets_scan_service.py`, `test_cancel_dual_write_service.py`, `test_reviewer_activity_publish_service.py`.
