# domain/findings

> Durable ticket-level findings, materialized at first report.

## Purpose

Owns the `pipeline_findings` table — every finding a review stage reports, from the moment it's first seen, including findings the fix loop resolves a minute later. One source of truth for finding content; the engine's `stage_executions.loop_state` holds only references + verdicts. Replaces reviewer-owned finding rows (`domain/reviewer`'s `findings` table, which stays alive during coexistence — see § Data owned for the table-name collision this creates). `record_findings`, the four transition functions (`resolve`/`reopen`/`dismiss`/`reflag`), `mark_defended`, `get`, the four list/lookup reads, `set_external_anchor`, `refresh_ticket_summary`, and `evaluate_auto_approve` are all real.

## Public interface

`Finding` (full VO with `handle` property, e.g. `SPEC-003`), `FindingStatusEvent` / `FindingSpec` / `AutoApproveConditions` (VOs), the function surface (`record_findings`, `resolve`, `reopen`, `dismiss`, `reflag`, `mark_defended`, `get`, `list_open_for_ticket`, `list_for_stage_execution`, `find_by_external_comment`, `set_external_anchor`, `refresh_ticket_summary`, `evaluate_auto_approve`), and `FindingNotFoundError` / `InvalidFindingTransition`. No HTTP routes — findings render inside run rows and posted-PR comments, never as a standalone surface.

## Module architecture

### Entities

- **Finding** — one durable finding, aggregate root. `severity` is immutable after creation; `status` transitions per the matrix below; `status_events` is an append-only JSONB trail (who/when/how per transition, including re-assertions).

### Key value objects

- **FindingSpec** — write input for `record_findings`; findings-owned so `pipelines → findings` stays one-way.
- **AutoApproveConditions** — the four Repos-page auto-approve checkboxes (stored by `domain/repos`, evaluated here).

### Core user flows

- **Materialize reported findings** — `record_findings` is called once per review return (`domain/pipelines`, main-loop pass or a standalone review stage), for every `FindingSpec` in the batch. Idempotent on `id`: a row that already exists (a re-report — same finding, later iteration) refreshes `body`/`code_file`/`code_line`/`artifact_section`/`defect_in_artifact` (latest wins) and is never duplicated; `severity` never changes after the first insert. `display_id` is the per-ticket running max+1, computed once for the whole batch — safe without a lock because one-run-per-ticket serializes writers (same convention as `domain/artifacts.store`'s version numbering). The handle (`f"{display_prefix}-{display_id:03d}"`) is exposed via `Finding.handle`.
- **Apply a transition** — `resolve`/`reopen`/`dismiss` move `status` per the matrix below; a call that targets the finding's *current* status is a no-op (no event, no audit row) — this is what makes verdict application safe under at-least-once task delivery. An illegal jump (anything not in the matrix) raises `InvalidFindingTransition`. `reflag` is different in kind: it doesn't change status (only legal from `open`) — it's a **re-assertion**, always appending an event even though nothing moved, recording "sighted again" / "fix claim verified false" for the trail.
- **Every transition + `mark_defended` audits** — `audit_for_finding` writes `finding.resolved` / `finding.reopened` / `finding.dismissed` / `finding.reflagged` / `finding.defended`, payload = the `FindingStatusEvent` (or a small stamp payload for `mark_defended`, which has no event input). `mark_defended` stamps `defended_at` once — idempotent, no-op on a second call.
- **Reads** — `get` (fetch one finding by id — the `github:update_pr` action resolves a `StageVerdict.finding_id` this way), `list_open_for_ticket` (ticket-wide, `status='open'` only — what a later review pass and the ticket summary read) and `list_for_stage_execution` (one stage execution's own findings, **any** status — the residual computation and the review-loop's own prior-findings union both read this). `find_by_external_comment` resolves a posted PR comment back to the finding it anchors.
- **Anchor a posted comment** — `set_external_anchor` stamps `external_comment_id` once a posting action (`plugins/github`'s `github:create_pr`/`github:update_pr`) posts the finding to the PR. Metadata only — no status transition, no audit row; idempotent (a posting action's own reconciliation against the VCS provider's existing comments guarantees it never anchors two different ids).
- **Ticket summary rollup** — `refresh_ticket_summary` counts this ticket's currently-`open` findings and the highest severity among them (`blocker > should_fix > nit`), and writes both onto `tickets.findings_count` / `tickets.max_severity` via `domain/tickets.update_findings_summary` — the same denormalized pair `domain/reviewer`'s own refresher feeds, during coexistence.
- **Auto-approve evaluation** — `evaluate_auto_approve(org_id, ticket_id, *, conditions, session)` scopes to findings *posted to the PR* (`external_comment_id` set) — a never-posted upstream residual doesn't factor in. `no_<severity>` passes iff zero posted findings of that severity are currently `open`; `all_confirmed_fixed` passes iff every posted, non-dismissed finding is `resolved` (i.e. none still `open`). A condition left off in `AutoApproveConditions` never gates, and no posted findings at all vacuously passes every enabled condition. Called by `domain/pr_review.evaluate_auto_approval` — see [domain_pr_review.md](domain_pr_review.md#core-user-flows).

### State machines

Finding status: `open → resolved` · `open → dismissed` · `resolved → open` (reopen) · `resolved → dismissed`. `dismissed` is terminal — a re-sighting after dismissal is a new finding. Transitions to the current status are idempotent no-ops (survives at-least-once task delivery); illegal jumps raise `InvalidFindingTransition`.

## Data owned

- `pipeline_findings` — one row per durable finding. `id` is app-minted (engine's uuid7 at first report, no `server_default`). `CHECK` constraints on `severity` and `status`. `UNIQUE(ticket_id, display_id)` backs the `<prefix>-<NNN>` handle.

**Table name note:** the table is `pipeline_findings`, not `findings` — `domain/reviewer` still owns a `findings` table for the coexistence period (both engines run side by side until `core/workflow` + `domain/reviewer` are deleted). Renames to `findings` once that table is retired.

## How it's tested

- `domain/pipelines/test/test_schema_service.py` seeds a minimal `pipeline_findings` row via raw SQL and asserts `status` defaults to `open` (schema-level check, predates this module's service functions).
- `test/test_transitions.py` (unit, real Postgres via `db_session`) — `record_findings` materializes `open` findings with the expected handle and per-ticket monotonic `display_id`; a re-report by `id` refreshes body/code_line while `severity` stays immutable; the full transition matrix (`resolve`/`reopen`/`dismiss`) including idempotent same-status no-ops (no duplicate event) and the `dismissed`-terminal invariant (every outbound transition — including `reflag` — raises `InvalidFindingTransition`); `reflag` appends an event without changing status and requires `open`.
- `domain/pipelines/test/test_review_loop_service.py` (`@pytest.mark.service`) exercises `record_findings` + the four transitions end-to-end through the run engine's review-fix loop — see [domain_pipelines.md § How it's tested](domain_pipelines.md#how-its-tested).
- `set_external_anchor`/`get` are exercised by the posting actions' own tests: `apps/backend/app/plugins/github/test/test_pr_actions_service.py` and `apps/backend/app/domain/pipelines/test/test_pr_actions_service.py` — see [domain_actions.md § How it's tested](domain_actions.md#how-its-tested).
