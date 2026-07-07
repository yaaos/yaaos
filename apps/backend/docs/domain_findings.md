# domain/findings

> Durable ticket-level findings, materialized at first report.

## Purpose

Owns the `pipeline_findings` table — every finding a review stage reports, from the moment it's first seen, including findings the fix loop resolves a minute later. One source of truth for finding content; the engine's `stage_executions.loop_state` holds only references + verdicts. Replaces reviewer-owned finding rows (`domain/reviewer`'s `findings` table, which stays alive during coexistence — see § Data owned for the table-name collision this creates). Does not yet own any runtime behavior — every `service.py` function raises `NotImplementedError`.

## Public interface

`Finding` (full VO with `handle` property, e.g. `SPEC-003`), `FindingStatusEvent` / `FindingSpec` / `AutoApproveConditions` (VOs), the stub function surface (`record_findings`, `set_external_anchor`, `resolve`, `reopen`, `dismiss`, `reflag`, `mark_defended`, `list_open_for_ticket`, `list_for_stage_execution`, `find_by_external_comment`, `evaluate_auto_approve`, `refresh_ticket_summary`), and `FindingNotFoundError` / `InvalidFindingTransition`. No HTTP routes — findings render inside run rows and posted-PR comments, never as a standalone surface.

## Module architecture

### Entities

- **Finding** — one durable finding, aggregate root. `severity` is immutable after creation; `status` transitions per the matrix below; `status_events` is an append-only JSONB trail (who/when/how per transition, including re-assertions).

### Key value objects

- **FindingSpec** — write input for `record_findings`; findings-owned so `pipelines → findings` stays one-way.
- **AutoApproveConditions** — the four Repos-page auto-approve checkboxes (stored by `domain/repos`, evaluated here).

### Core user flows

Every service function raises `NotImplementedError` — the table and signatures are the module's current substance.

### State machines

Finding status: `open → resolved` · `open → dismissed` · `resolved → open` (reopen) · `resolved → dismissed`. `dismissed` is terminal — a re-sighting after dismissal is a new finding. Transitions to the current status are idempotent no-ops (survives at-least-once task delivery); illegal jumps raise `InvalidFindingTransition`.

## Data owned

- `pipeline_findings` — one row per durable finding. `id` is app-minted (engine's uuid7 at first report, no `server_default`). `CHECK` constraints on `severity` and `status`. `UNIQUE(ticket_id, display_id)` backs the `<prefix>-<NNN>` handle.

**Table name note:** the table is `pipeline_findings`, not `findings` — `domain/reviewer` still owns a `findings` table for the coexistence period (both engines run side by side until `core/workflow` + `domain/reviewer` are deleted). Renames to `findings` once that table is retired.

## How it's tested

- `domain/pipelines/test/test_schema_service.py` seeds a minimal `pipeline_findings` row via raw SQL (this module's service functions don't exist yet to drive it through the public API) and asserts `status` defaults to `open`.
