# domain/pipelines

> The run engine: data-defined pipelines, run + stage lifecycle, HITL pauses.

## Purpose

Owns the four tables backing the pipelines run engine (`pipelines`, `pipeline_runs`, `stage_executions`, `run_pauses`), the definition model + validation, and the CRUD surface behind `/api/pipelines`. This module replaces `core/workflow` + `domain/reviewer`'s workflow-engine role; both stay alive and untouched during coexistence (`core/workflow`, `domain/reviewer`, `pr_review_v1`). Definition CRUD is real; run-lifecycle behavior (`start_run`, `resolve_pause`, ...) is still a stub — those bodies raise `NotImplementedError`.

## Public interface

Definition-model value objects (`PipelineDefinition`, `Stage` discriminated union — `SkillStage | ReviewSkillStage | ActionStage | PipelineCallStage`, `BoundaryControl`, `ReviewConfig`), stored-entity VOs (`Pipeline`, `PipelineSummary`), run-lifecycle value objects (`Kickoff`, `PipelineRun`, `StageExecution`, `RunOverview`, `PauseResolution`), the function surface (`create_pipeline`, `update_pipeline`, `delete_pipeline`, `get_pipeline`, `list_pipelines`, `pipeline_referenced_by_call` — real; `start_run`, `start_rerun_from_stage`, `request_cancel`, `resolve_pause`, `instantiate_template`, `list_templates`, `list_runs_for_ticket`, `get_run_overview`, `has_run_in_flight` — stub), and the typed error hierarchy (`PipelineNotFoundError`, `PipelineNameTakenError`, `PipelineValidationError`, `PipelineReferencedError`, `RunNotFoundError`, `PauseNotFoundError`, `PauseAlreadyResolvedError`, `NotEscalationTargetError`, `StageNotInDefinitionError`, `MissingInheritedArtifactError`).

`flatten()` and `validate_definition()` (in `definition.py`) are internal to the module — not re-exported — since only `create_pipeline`/`update_pipeline` call them.

HTTP routes: `GET/POST /api/pipelines`, `GET/PUT/DELETE /api/pipelines/{id}` — see § HTTP endpoints below. Run-lifecycle routes (`/api/pipelines/runs/...`) land with the run engine.

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

### State machines

- **Run state** (target shape, not yet enforced by code): `queued → running ⇄ paused → completed | failed | killed | cancelled`.
- **Run phase**: `provision → stages → cleanup`.

## HTTP endpoints

`domain/pipelines/web.py`, prefix `/api/pipelines`, all `ORG_SCOPED`, gated by the single `Action.PIPELINES_MANAGE` (admin minimum — reads and writes both, unlike the READ/WRITE-split settings actions elsewhere).

| Method | Path | Response | Errors |
|---|---|---|---|
| GET | `/api/pipelines` | 200 `{pipelines: PipelineSummary[]}` | 403 |
| POST | `/api/pipelines` | 201 `{id}` | 400 `invalid_definition` · 409 `name_taken` |
| GET | `/api/pipelines/{id}` | 200 flat `{id, name, description, stages, updated_at, updated_by_login, referenced}` | 404 |
| PUT | `/api/pipelines/{id}` | 200 (same flat shape) | 400 `invalid_definition` · 404 · 409 `name_taken` |
| DELETE | `/api/pipelines/{id}` | 204 | 404 · 409 `referenced` |

POST/PUT bodies are the `PipelineDefinition` model itself — no separate write-request shape.

## Data owned

- `pipelines` — org-scoped pipeline definitions. `UNIQUE(org_id, name)`. `id` has no DB-level default (unlike its sibling tables below) — the pipeline's own id participates in `PipelineCallStage.pipeline_id` and templates ship with pinned ids, so `create_pipeline` mints it app-side via the definition model's `id` field (itself defaulting to a fresh uuid7 when the request omits it). See `apps/backend/docs/patterns.md` § UUID primary keys.
- `pipeline_runs` — one row per run. `UNIQUE INDEX ux_pipeline_runs_one_in_flight ON (ticket_id) WHERE state IN ('running','paused')` enforces one in-flight run per ticket at the DB layer.
- `stage_executions` — one row per stage-execution attempt. `CHECK` constraints on `kind`, `status`, `phase`, `confidence`, `boundary_outcome`.
- `run_pauses` — one row per HITL pause. `escalation_user_ids` is a `UUID[]`.

## How it's tested

- `test/test_schema_service.py` (`@pytest.mark.service`) — the four owned tables accept a minimal insert; the one-in-flight partial unique index rejects a second concurrently-`running` run on the same ticket. Also seeds the five sibling-owned tables (`artifacts`, `pipeline_findings`, `repo_settings`, `repo_trigger_bindings`, `pr_comments`) via raw SQL to verify the full migrated schema end-to-end, since none of those modules' service functions exist yet to drive through their public API.
- `test/test_definition_flatten.py` (unit) — `flatten()`/`validate_definition()` over in-memory definition maps: nested call expansion, self + mutual cycles, unknown call targets, duplicate flattened stage names, and multi-hop transitive-caller revalidation on a callee edit.
- `test/test_pipeline_crud_service.py` (`@pytest.mark.service`) — full CRUD via `httpx.ASGITransport`: create + round-trip, cycle rejection, name-collision rejection, referenced-delete rejection, update, list, role gating (admin vs builder vs unauthenticated), and audit rows (`pipeline.created`, `pipeline.updated`, `pipeline.deleted`).
