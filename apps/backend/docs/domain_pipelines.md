# domain/pipelines

> The run engine: data-defined pipelines, run + stage lifecycle, HITL pauses.

## Purpose

Owns the four tables backing the pipelines run engine (`pipelines`, `pipeline_runs`, `stage_executions`, `run_pauses`), the definition model + validation, the CRUD surface behind `/api/pipelines`, and the run engine itself (`engine.py`) — the `ROUTE_RUN`/`START_STAGE` taskiq trio that drives a `PipelineRun` from `queued` through `action`-stage execution to a terminal state. This module replaces `core/workflow` + `domain/reviewer`'s workflow-engine role; both stay alive and untouched during coexistence (`core/workflow`, `domain/reviewer`, `pr_review_v1`). Definition CRUD, `start_run`, and `request_cancel` are real. Skill/review stage dispatch isn't built yet — a run that reaches one fails loudly rather than hanging (see § Core user flows). `start_rerun_from_stage`, `resolve_pause`, `instantiate_template`, `list_templates`, `list_runs_for_ticket`, `get_run_overview`, and `has_run_in_flight` are still stubs raising `NotImplementedError`.

## Public interface

Definition-model value objects (`PipelineDefinition`, `Stage` discriminated union — `SkillStage | ReviewSkillStage | ActionStage | PipelineCallStage`, `BoundaryControl`, `ReviewConfig`), stored-entity VOs (`Pipeline`, `PipelineSummary`), run-lifecycle value objects (`Kickoff`, `PipelineRun`, `StageExecution`, `RunOverview`, `PauseResolution`), the function surface (`create_pipeline`, `update_pipeline`, `delete_pipeline`, `get_pipeline`, `list_pipelines`, `pipeline_referenced_by_call`, `start_run`, `request_cancel` — real; `start_rerun_from_stage`, `resolve_pause`, `instantiate_template`, `list_templates`, `list_runs_for_ticket`, `get_run_overview`, `has_run_in_flight` — stub), and the typed error hierarchy (`PipelineNotFoundError`, `PipelineNameTakenError`, `PipelineValidationError`, `PipelineReferencedError`, `RunNotFoundError`, `RunAlreadyTerminalError`, `PauseNotFoundError`, `PauseAlreadyResolvedError`, `NotEscalationTargetError`, `StageNotInDefinitionError`, `MissingInheritedArtifactError`).

`flatten()`, `validate_definition()`, and `FlattenedDefinition.from_snapshot()` (in `definition.py`) are internal to the module — not re-exported. `engine.py` (the `ROUTE_RUN`/`START_STAGE` task bodies, `attempt_promotion`, `promote_oldest_queued`, `cancel_queued`) and `escalation.py` (`resolve_escalation_targets`) are likewise intra-module only — `service.py`'s `start_run`/`request_cancel` are the sole public entry points onto the engine.

HTTP routes: `GET/POST /api/pipelines`, `GET/PUT/DELETE /api/pipelines/{id}`, `POST /api/pipelines/runs/{run_id}/cancel` — see § HTTP endpoints below. Further run-lifecycle routes (pause respond, rerun, run/overview reads) land with the rest of the run engine.

## Module architecture

### Entities

- **Pipeline** — the stored org pipeline: a `PipelineDefinition` plus persistence metadata (`updated_at`, `updated_by_login`, `referenced`).
- **PipelineRun** — one run of a pipeline against a ticket; replaces `WorkflowExecution`. Pins a flattened definition snapshot at start.
- **StageExecution** — one execution attempt of one stage (or a `kind='system'` bookkeeping row — provision/cleanup/auth-refresh/push-branch).
- **RunPause** — one HITL pause; replaces `PendingHumanDecision`.

### Key value objects

- **PipelineDefinition** — the authored content (`id`, `name`, `description`, `stages`); a discriminated union on `kind` (`skill | review | action | call`). `id` (top-level and per-stage) defaults to a fresh uuid7 at parse time (`Field(default_factory=uuid7)`) — a request body that omits `id` on a new pipeline or a newly-added stage gets one server-minted for free.
- **Kickoff** — intake point + actor + input text that started a run; the ticket (with title) exists before the run.
- **BoundaryControl** — flat per-stage "what to do next" setting (`always_hitl | always_proceed | conditional`).
- **RunOverview** — server-computed Overview-tab payload, tagged `paused | in_flight | terminal`.

### Core user flows

- **Create/update a pipeline** — `create_pipeline`/`update_pipeline` dry-run `validate_definition()`: flattens the edited definition AND every org pipeline that transitively calls it (via `PipelineCallStage`), against a supplied org-wide id → definition map. A call cycle, an unresolvable call target, or a duplicate flattened stage name (skill/review stages only — action/call stages carry no name) raises `PipelineValidationError` before anything is written. Storage never flattens — `stages` keeps `PipelineCallStage` entries as-is, so a callee's edits reach every caller's *future* runs; the real per-run flatten is the run engine's job.
- **Delete a pipeline** — refused with `PipelineReferencedError` when `pipeline_referenced_by_call` (an app-side scan of the org's `stages` JSONB for a `call` stage targeting this pipeline) or `repos.pipeline_referenced_by_binding` (stub — always `False` until repo trigger bindings exist) is true.
- **Read** — `get_pipeline`/`list_pipelines` resolve `updated_by_login` via `core/tenancy.get_membership_info` (the org membership handle, not a global username) and `referenced` via the same OR check delete uses.
- **Start a run** — `start_run` flattens the pipeline against *current* org definitions and pins the result as `pipeline_runs.definition_snapshot`. The row always inserts `state='queued'` first; `engine.attempt_promotion` then attempts a conditional `queued -> running` flip guarded by `ux_pipeline_runs_one_in_flight` (SAVEPOINT-wrapped — an `IntegrityError` from a concurrent promotion on the same ticket leaves the run `queued`, never raises). A successful promotion stamps `tickets.current_run_id`, flips the ticket `pending -> running` (`transition_ticket_on_run_start`), writes `run.started`, publishes `run_state_changed`, and enqueues the bootstrap `ROUTE_RUN`.
- **Run a stage** — `ROUTE_RUN` decides the next boundary action (dispatch stage 0, dispatch the next stage, or enter a terminal state); `START_STAGE` executes one stage and reports back to `ROUTE_RUN`. Only `kind='action'` stages dispatch for real: `Action.execute` runs inside a SAVEPOINT (the `stage_executions` row's `status`/`action_result`/`failure_reason` land atomically with the action's own writes). A `kind='skill'`/`'review'` stage fails the run immediately with a named `failure_reason` — the coding-agent invocation wiring these need doesn't exist in this engine yet, so a run must not hang waiting for an agent command that will never be sent. Because of that, `phase` never leaves `'stages'` here: workspace provision/cleanup are `kind='system'` rows the skill-stage machinery creates, and an action-only pipeline has nothing for them to do — this is also the permanent behavior for zero-skill pipelines, not a temporary gap.
- **Reach a terminal state** — `_enter_terminal` writes the terminal `pipeline_runs.state`, flips the ticket to the matching status (`transition_ticket_on_run_terminal`), writes `run.{state}`, publishes `run_state_changed`, notifies the resolved escalation targets on `failed` (`escalation.resolve_escalation_targets` — kickoff actor, else schedule notify list, else the PR author's linked identity, else the org's admins), and promotes the oldest `queued` run on the ticket (`promote_oldest_queued`).
- **Cancel a run** — `request_cancel`: `queued` cancels immediately (`engine.cancel_queued`); `running` sets `cancel_requested` and the next `ROUTE_RUN` boundary check routes to `cancelled` instead of the next stage — checked on every boundary including the last one, so a run cannot slip into `completed` after cancellation was requested. Raises `RunAlreadyTerminalError` on an already-terminal run (409 at HTTP); the `paused` branch isn't reachable yet (no pause can exist — boundary evaluation always proceeds).

### State machines

- **Run state**: `queued → running ⇄ paused → completed | failed | killed | cancelled`. This engine drives `queued → running → completed | failed | cancelled` for good; `paused`/`killed` exist on the row's `CHECK` constraint but nothing produces them yet (boundary evaluation is a labeled stub that always proceeds).
- **Run phase**: `provision → stages → cleanup`. Stays at `'stages'` in this engine — see § Core user flows.

## HTTP endpoints

`domain/pipelines/web.py`, prefix `/api/pipelines`, all `ORG_SCOPED`. Definition CRUD is gated by `Action.PIPELINES_MANAGE` (admin minimum); the run-cancel endpoint is gated by `Action.REVIEWER_WRITE` (builder minimum) — a run being watched on a ticket is a builder-facing concern, not a pipeline-authoring one.

| Method | Path | Response | Errors |
|---|---|---|---|
| GET | `/api/pipelines` | 200 `{pipelines: PipelineSummary[]}` | 403 |
| POST | `/api/pipelines` | 201 `{id}` | 400 `invalid_definition` · 409 `name_taken` |
| GET | `/api/pipelines/{id}` | 200 flat `{id, name, description, stages, updated_at, updated_by_login, referenced}` | 404 |
| PUT | `/api/pipelines/{id}` | 200 (same flat shape) | 400 `invalid_definition` · 404 · 409 `name_taken` |
| DELETE | `/api/pipelines/{id}` | 204 | 404 · 409 `referenced` |
| POST | `/api/pipelines/runs/{run_id}/cancel` | 202 | 404 · 409 `terminal` |

POST/PUT bodies are the `PipelineDefinition` model itself — no separate write-request shape.

## Data owned

- `pipelines` — org-scoped pipeline definitions. `UNIQUE(org_id, name)`. `id` has no DB-level default (unlike its sibling tables below) — the pipeline's own id participates in `PipelineCallStage.pipeline_id` and templates ship with pinned ids, so `create_pipeline` mints it app-side via the definition model's `id` field (itself defaulting to a fresh uuid7 when the request omits it). See `apps/backend/docs/patterns.md` § UUID primary keys.
- `pipeline_runs` — one row per run. `UNIQUE INDEX ux_pipeline_runs_one_in_flight ON (ticket_id) WHERE state IN ('running','paused')` enforces one in-flight run per ticket at the DB layer; the run engine's promotion mechanics are built directly on this index.
- `stage_executions` — one row per stage-execution attempt. `CHECK` constraints on `kind`, `status`, `phase`, `confidence`, `boundary_outcome`. `action_result` is the `Action.Result` dump for `kind='action'` rows.
- `run_pauses` — one row per HITL pause. `escalation_user_ids` is a `UUID[]`. No rows exist yet — nothing creates a pause.

## How it's tested

- `test/test_schema_service.py` (`@pytest.mark.service`) — the four owned tables accept a minimal insert; the one-in-flight partial unique index rejects a second concurrently-`running` run on the same ticket. Also seeds the five sibling-owned tables (`artifacts`, `pipeline_findings`, `repo_settings`, `repo_trigger_bindings`, `pr_comments`) via raw SQL to verify the full migrated schema end-to-end, since none of those modules' service functions exist yet to drive through their public API.
- `test/test_definition_flatten.py` (unit) — `flatten()`/`validate_definition()` over in-memory definition maps: nested call expansion, self + mutual cycles, unknown call targets, duplicate flattened stage names, and multi-hop transitive-caller revalidation on a callee edit.
- `test/test_pipeline_crud_service.py` (`@pytest.mark.service`) — full CRUD via `httpx.ASGITransport`: create + round-trip, cycle rejection, name-collision rejection, referenced-delete rejection, update, list, role gating (admin vs builder vs unauthenticated), and audit rows (`pipeline.created`, `pipeline.updated`, `pipeline.deleted`).
- `test/test_run_lifecycle_service.py` (`@pytest.mark.service`) — a two-action-stage run completes end-to-end through the `ROUTE_RUN`/`START_STAGE` trio: both `stage_executions` rows carry `action_result`, `run.started`/`run.completed` audit rows exist, `run_state_changed` publishes over SSE (via `test/drain.py`'s outbox-dispatch helper + `set_actions_for_tests`). A second test drives an `ActionError` to a `run.failed` terminal + a notification to the kickoff actor.
- `test/test_run_queueing_service.py` (`@pytest.mark.service`) — a second run started while the first is in flight sits `queued` until the first's terminal promotes it; three runs on one ticket (one `running`, two `queued`) prove the promotion-race guard directly (`attempt_promotion` on an already-occupied ticket returns `False`, never raises); cancel of a `running` run defers to the next boundary; cancel of a `queued` run is immediate.
