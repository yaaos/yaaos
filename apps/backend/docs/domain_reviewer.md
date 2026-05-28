# domain/reviewer

> Review workflow orchestrator + durable findings. The workflow engine drives every review run; `PRReviewAggregate` owns `Finding` / state machine / acknowledgments / threads as the durable layer.

## Scope

Owns every artifact tied to "what yaaos has said about a PR": `Review`s, `Finding`s, `FindingObservation`s, `CommentThread`s, `CommentMessage`s, `AcknowledgmentDecision`s.

Does NOT call an LLM for code review — `domain/coding_agent` plugins do that. The only direct LLM call here is the reply classifier, via `core/llm`.

## Workflows + commands

Five workflows in `domain/reviewer/workflows/`, five matching Workspace commands and five Local commands in `commands/`. See [`commands/__init__.py`](../app/domain/reviewer/commands/__init__.py) for bodies.

- `pr_review_v1` — `CheckShouldReview → SecretsScan → ProvisionWorkspace → CodeReview → PostFindings → CleanupWorkspace`
- `incremental_review_v1` — same shape with `IncrementalReview` substituted for `CodeReview`
- `verify_fix_v1` — `ProvisionWorkspace → VerifyFix → ResolveFinding → CleanupWorkspace`
- `stale_check_v1` — `ProvisionWorkspace → StaleCheck → ArchiveStaleFindings → CleanupWorkspace`
- `answer_question_v1` — `ProvisionWorkspace → AnswerQuestion → PostReply → CleanupWorkspace`

`ProvisionWorkspace` / `CleanupWorkspace` / `RefreshWorkspaceAuth` come from [`core/workspace.commands`](core_workspace.md).

`CheckShouldReview` returns `skip` on: draft, fork, `yaaos-skip`/`no-review`/`wip` labels (case-insensitive), `*[bot]`/`*-bot` author. `CleanupWorkspace` runs as the workflow's `final` step regardless of upstream failure.

## Core flows

For the top-level review arc see [`docs/system-architecture.md`](../../../docs/system-architecture.md). Reviewer-internal detail only:

**Full review (`pr_review_v1`):** `CodeReview` → `PostFindings`. `CodeReview` builds an MCP payload via `mcp_wiring.build_mcp_payload`, calls `coding_agent.review`, emits `draft_findings` + `summary_body`. `PostFindings` deserializes drafts → runs admission → posts survivors via the VCS plugin → persists `external_comment_id`. Rejected drafts never reach GitHub.

**Incremental push:** trigger policy returns `Skip | Debounce | Run`. On `Run`: runs `resolve_open_anchors` (deterministic, no LLM) to partition open findings into `moved / gone / unchanged` — `gone` → `resolved_unverified`, `moved` → `verify_fix`. New findings flow through admission. Anchor mutations are computed on a snapshot aggregate and replayed onto a freshly-loaded aggregate at save time.

**Developer reply:** `is_yaaos_command` → command path; `is_off_topic_message` → store only. Otherwise `classify_reply` → one of five categorical intents → `apply_classified_reply`:

| intent | action |
|---|---|
| `acknowledgment_clear` | `acknowledge_posted` — state → acknowledged + reply |
| `acknowledgment_unclear` | `confirm_requested` — post mid-band confirm prompt |
| `verify_fix` | `verify_fix_triggered` — spawn workspace subflow |
| `question` | `answer_question_triggered` — spawn workspace subflow |
| `other` | `noop` |

When developer responds `confirm` to a mid-band prompt, `_original_mid_band_rationale` in `replies.py` walks the thread to find the last human message from that author BEFORE the confirm-request and uses its body as the persisted ack rationale.

**Verify-fix / stale-check:** provisions workspace at HEAD, reads current code at resolved anchor, hands to `coding_agent.verify_fix` / `coding_agent.stale_check`. Result feeds `apply_verify_fix_result` / `apply_stale_check_result`.

**Answer question:** read-only workspace access (no `Task` subagent), passes finding context + thread history + question to `coding_agent.answer_question`, posts the answer as a yaaos reply. No state transition.

## Admission pipeline

Inside `aggregate.post_process_raw_findings(review_id, raw, *, diff_files=None)`, in order:

1. **Schema gate** — drop if `concrete_failure_scenario` missing or < 20 chars. Reason: `malformed`.
2. **Off-diff drop** — drop if anchor file not in PR diff (when `diff_files` supplied). Reason: `off_diff`.
3. **Per-severity threshold** — `blocker`/`major` ≥ 75, `minor` ≥ 85, `nit` ≥ 90. Reason: `below_threshold`.
4. **Per-PR nit cap** — at most 5 nits ever per PR. Reason: `nit_cap`.
5. **Fingerprint match** — vs prior findings: `acknowledged` match → drop (`matches_ack`); `open` match → re-observe (sticky severity, `max` confidence).
6. **Cross-file dedup** — same-rule findings across files collapse to one; body gains "Also in: …" footer.
7. **Per-review top-10 cap** — rank by `severity_weight × confidence` (blocker=4, major=3, minor=2, nit=1); admit top 10. Re-observations exempt. Reason: `top_cap`.

Admission runs BEFORE `vcs.post_review` in `PostFindings`.

## State machine

`FindingState`: `open → acknowledged | resolved_confirmed | resolved_unverified | stale` (all four are terminal today).

- `(new) → open` — new fingerprint observed.
- `open → acknowledged` — `acknowledgment_clear` reply. `acknowledgment_unclear` does NOT transition — it waits for literal `confirm`.
- `open → resolved_confirmed` — verify-fix returns "not present" with confidence ≥ 0.80 (`VERIFY_ACT_THRESHOLD`).
- `open → resolved_unverified` — anchor gone in new commit, no verify-fix possible.
- `open → stale` — stale-check returns "no longer applies" with confidence ≥ 0.80.

Low-confidence agent output never causes a state change. Pure transition functions in `state_machine.py`; aggregate is the only legitimate caller.

## Invariants + why

- **Advisory lock first.** `lock.acquire_pr_lock` issues `pg_advisory_xact_lock(hashtext('pr:<uuid>')::bigint)` inside the transaction before any aggregate load. Two concurrent webhooks for the same PR serialize; lock releases on commit/rollback. Read-only paths do NOT take the lock.
- **`sequence_number` assigned under the lock.** Both `incremental_trigger.py` (push-incremental) and `save` (full review) INSERT the `ReviewRow` inside the per-PR lock. This is the only safe place to assign the per-PR ordinal.
- **FK flush order in `save`.** `findings → flush → observations + threads → flush → messages → flush → acks`. Violating this order hits FK violations.
- **`dispatch_events` before `session.commit()`.** `dispatch_events` stashes domain events; `publish_general_after_commit` fires them post-commit. Rolled-back transactions silently discard the stash — no phantom SPA events.
- **`dispatch_audits` before commit too.** Writes one `audit_entries` row per finding state-change event. Both helpers must precede the commit.
- **`review_id` columns on findings tables are unconstrained UUIDs** — not FK-constrained by design. `ReviewJob` is a read-side projection over `workflow_executions`, not its own table.
- **`original_lines` persisted in anchor JSONB.** Captured at finding-creation by `make_anchor()`, carried forward by `resolve_anchor()`, read by verify-fix. No separate column.

## Data owned

- `reviews` — one row per PR run. `sequence_number` (per-PR ordinal), `trigger_reason`, `scope_kind/prev_sha`, `commit_sha_at_start`, `superseded_by_review_id`, `pending_replay`. See `models.py` + [core_database.md](core_database.md) for columns.
- `findings` — UNIQUE `(pr_id, fingerprint_hash)`. State, sticky severity, max-confidence, anchor JSONB, `concrete_failure_scenario`.
- `finding_observations` — append-only `(finding × review)` sightings.
- `comment_threads` — 1:1 with findings. `external_thread_id` indexed for webhook resolution.
- `comment_messages` — every yaaos- and human-authored message. `external_comment_id` indexed. `classified_intent` on human messages.
- `acknowledgment_decisions` — persistent dev decisions; survive future reviews.

## Vocabulary

- `FindingFingerprint` — `(file_path, rule_id, anchor_content_hash, body_gist_hash)`. Whitespace-normalized so reindents don't churn fingerprints.
- `CodeAnchor` — `(file_path, line_start, line_end, surrounding_content_hash, commit_sha)`. Surrounding hash covers ±3 lines; lets `anchor.resolve_anchor` re-find position after line drift.
- `RawFinding` — coding-agent output before admission; must include `concrete_failure_scenario` ≥ 20 chars.
- `AdmissionDrop` — audit payload for a rejected raw finding: `(rule_id, reason, severity, confidence)`.
- `ReviewJob` — read-side projection over `workflow_executions` (not a DB table). Built by `workflow_review_view.py`.

## How it's tested

- **Unit** — `state_machine.py`, `fingerprint.py`, `anchor.py`, `trigger.py`, aggregate, service helpers, classifier (canned-output runnable).
- **In-memory `AggregateRepository`** (`test/in_memory_repository.py`) — admission pipeline, state transitions, round-trip persistence.
- **Service tests** (`@pytest.mark.service`) — `test_pr_review_v1_e2e_service.py` (full pipeline, stub VCS + coding-agent + workspace), `test_mcp_review_pipeline_service.py`, `test_secrets_scan_service.py`, `test_cancel_dual_write_service.py`, `test_all_workflows_smoke.py` (all 5 workflows), `test_reviewer_activity_publish_service.py`.
- **Evals** — `classify_reply` prompt evals under `domain/reviewer/eval/`; always hit the model fresh (bypass `SQLiteCache`).
