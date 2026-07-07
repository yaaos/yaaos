# domain/actions

> Synchronous deterministic control-plane stage executors + registry.

## Purpose

Owns the `Action` Protocol and its ContextVar-bound registry — mirroring `CodingAgentRegistry` (`apps/backend/app/core/coding_agent/service.py:27`). Plugins (e.g. `plugins/github`) contribute `Action`s at import time via `register_action`; `ActionStage.action_id` (owned by `domain/pipelines`) keys into this registry. Lives in `domain`, not `core`, because `ActionContext` carries findings/verdicts (domain types) and core→domain edges are forbidden. No tables — an action's result persists on `stage_executions.action_result`, owned by `domain/pipelines`.

## Public interface

`Action` (Protocol), `ActionContext`, `StageVerdict`, `ActionInfo` (value objects), `ActionError` / `ActionNotFoundError`, and the registry functions `register_action`, `get_action`, `list_actions`, `set_actions_for_tests`. HTTP: `GET /api/actions` (`Action.PIPELINES_MANAGE`) — read-only picker mirror returning `{actions: [{action_id, plugin_id, label}]}`, the Pipelines-page "Add an action" data source.

## Module architecture

### Key value objects

- **ActionContext** — flattened control-plane context handed to `Action.execute`: org/ticket/run ids, repo + VCS plugin id, optional PR id, branch name (already pushed by the stage's exit-push), the preceding stage's residual findings + verdicts + artifact id. Imports `domain/findings` only, so `pipelines → actions → findings` stays strictly one-way.
- **StageVerdict** — actions-owned mirror of a recorded review verdict (`finding_id`, `status`, `reply`).

### Core user flows

1. A plugin registers an `Action` at import time via `register_action`.
2. `domain/pipelines`' run engine (`engine.py`'s `START_STAGE` task body) resolves `action_id` via `get_action` for every `kind='action'` stage, builds an `ActionContext`, and calls `execute` inside a SAVEPOINT.
3. `execute` runs synchronous, deterministic control-plane code (no parking, no boundary control, no artifact, no confidence) and returns a typed `Result`; a raised `ActionError` fails the run.

`plugins/github` registers two actions at import time (`plugins/github/actions.py`, `bootstrap()`): `github:create_pr` opens the PR for a yaaos-authored ticket's work branch (idempotent — `vcs.create_pr`'s find-existing-for-branch fallback resolves the same PR on retry; a ticket that already has a PR skips straight to posting) then posts the preceding stage's residual findings; `github:update_pr` posts new residuals the same way and reflects the preceding review stage's mechanically-applied verdicts back onto the PR (resolves the thread of every `fixed` finding via `vcs.resolve_finding_thread`, posts any verdict `reply` text into the finding's own thread). Both share `_post_residuals`, the posting primitive: before posting a not-yet-anchored residual it reconciles against `vcs.list_yaaos_comments`, matching on the finding's own `handle` (embedded verbatim in the `rule_violated` argument to `vcs.post_finding`) — a mid-body crash that posted a GitHub comment before the DB write anchoring it landed is discovered on retry, not double-posted. `github:reply_to_comment` (the conversation-policy action) lands with the comment feedback loop. Test actions exercise the dispatch path in isolation (`domain/pipelines/test/test_run_lifecycle_service.py`, `test_run_queueing_service.py`).

### State machines

None — the registry is a flat id→`Action` map.

## Data owned

None. Action results persist on `stage_executions.action_result` (owned by `domain/pipelines`).

## How it's tested

- `test/test_registry.py` — register/get/list round-trip, duplicate-id rejection (`ValueError`), unknown-id lookup (`ActionNotFoundError`), and `set_actions_for_tests` isolation (`default` scenario copies + restores the prior binding; `empty` scenario isolates from the current registry).
- Shipped `github:create_pr`/`github:update_pr` coverage lives with their owning modules: `plugins/github/test/test_pr_actions_service.py` (idempotency-on-retry, posting reconciliation after a simulated crash — entry point is `Action.execute`) and `domain/pipelines/test/test_pr_actions_service.py` (full-engine acceptance — entry point is `pipelines.start_run`). See [domain_pipelines.md § How it's tested](domain_pipelines.md#how-its-tested).
