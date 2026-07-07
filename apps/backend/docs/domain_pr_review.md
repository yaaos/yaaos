# domain/pr_review

> Inbound free-text PR comment classification + batching.

## Purpose

Owns the `pr_comments` table — every inbound free-text PR comment yaaos tracks, from classification through batching into a comment-response run. Lifecycle is derived, not a status column. Every `service.py` function raises `NotImplementedError` — the table and signatures are the module's current substance.

## Public interface

`PRComment` (full VO), `InboundComment` (VCS-agnostic wire input from the plugin), and the stub function surface (`handle_pr_comment`, `maybe_start_batch_run`, `evaluate_auto_approval`, `list_comments_for_run`). No HTTP routes — this module has no UI surface; it's driven by the VCS plugin's webhook handler.

## Module architecture

### Entities

- **PRComment** — one inbound free-text PR comment. `finding_id` is set when the comment replies to a finding thread (resolved via `findings.find_by_external_comment`); `classification` is `null` until classified.

### Key value objects

- **InboundComment** — the VCS-agnostic shape the plugin hands in (external id, author, body, in-reply-to).

### Core user flows

Every service function raises `NotImplementedError` — the table and signatures are the module's current substance.

### State machines

Comment lifecycle is derived from column state, not a status enum: `NULL classification` = awaiting classify · `unclear` = terminal (canned reply) · classified + unclaimed = waiting · claimed = in a run.

## Data owned

- `pr_comments` — one row per inbound comment. `UNIQUE(org_id, comment_external_id)`. `CHECK` constraint on `classification`. `finding_id` FK → `pipeline_findings(id)` (owned by `domain/findings`).

## How it's tested

- `domain/pipelines/test/test_schema_service.py` seeds a minimal `pr_comments` row via raw SQL (this module's service functions don't exist yet to drive it through the public API) and asserts `classification` defaults to `null`.
