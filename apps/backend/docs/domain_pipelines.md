# domain/pipelines

> The run engine: data-defined pipelines, run + stage lifecycle, HITL pauses.

## Purpose

Owns the four tables backing the pipelines run engine (`pipelines`, `pipeline_runs`, `stage_executions`, `run_pauses`) and the definition + read-model value objects the engine's public surface will speak. This module replaces `core/workflow` + `domain/reviewer`'s workflow-engine role; both stay alive and untouched during coexistence (`core/workflow`, `domain/reviewer`, `pr_review_v1`). Does not yet own any runtime behavior — every `service.py` function raises `NotImplementedError`; the run-lifecycle engine phases fill these in.

## Public interface

Definition-model value objects (`PipelineDefinition`, `Stage` discriminated union — `SkillStage | ReviewSkillStage | ActionStage | PipelineCallStage`, `BoundaryControl`, `ReviewConfig`, `Pipeline`, `PipelineSummary`), run-lifecycle value objects (`Kickoff`, `PipelineRun`, `StageExecution`, `RunOverview`, `PauseResolution`), and the stub function surface (`start_run`, `start_rerun_from_stage`, `request_cancel`, `resolve_pause`, `create_pipeline`, `update_pipeline`, `delete_pipeline`, `get_pipeline`, `list_pipelines`, `instantiate_template`, `list_templates`, `pipeline_referenced_by_call`, `list_runs_for_ticket`, `get_run_overview`, `has_run_in_flight`) plus the typed error hierarchy (`PipelineNotFoundError`, `PipelineValidationError`, `PipelineReferencedError`, `RunNotFoundError`, `PauseNotFoundError`, `PauseAlreadyResolvedError`, `NotEscalationTargetError`, `StageNotInDefinitionError`, `MissingInheritedArtifactError`). No HTTP routes yet.

## Module architecture

### Entities

- **Pipeline** — the stored org pipeline: a `PipelineDefinition` plus persistence metadata (`updated_at`, `updated_by_login`, `referenced`).
- **PipelineRun** — one run of a pipeline against a ticket; replaces `WorkflowExecution`. Pins a flattened definition snapshot at start.
- **StageExecution** — one execution attempt of one stage (or a `kind='system'` bookkeeping row — provision/cleanup/auth-refresh/push-branch).
- **RunPause** — one HITL pause; replaces `PendingHumanDecision`.

### Key value objects

- **PipelineDefinition** — the authored content (`id`, `name`, `description`, `stages`); a discriminated union on `kind` (`skill | review | action | call`).
- **Kickoff** — intake point + actor + input text that started a run; the ticket (with title) exists before the run.
- **BoundaryControl** — flat per-stage "what to do next" setting (`always_hitl | always_proceed | conditional`).
- **RunOverview** — server-computed Overview-tab payload, tagged `paused | in_flight | terminal`.

### Core user flows

Every service stub raises `NotImplementedError` — the tables and public signatures are the module's current substance.

### State machines

- **Run state** (target shape, not yet enforced by code): `queued → running ⇄ paused → completed | failed | killed | cancelled`.
- **Run phase**: `provision → stages → cleanup`.

## Data owned

- `pipelines` — org-scoped pipeline definitions. `UNIQUE(org_id, name)`.
- `pipeline_runs` — one row per run. `UNIQUE INDEX ux_pipeline_runs_one_in_flight ON (ticket_id) WHERE state IN ('running','paused')` enforces one in-flight run per ticket at the DB layer.
- `stage_executions` — one row per stage-execution attempt. `CHECK` constraints on `kind`, `status`, `phase`, `confidence`, `boundary_outcome`.
- `run_pauses` — one row per HITL pause. `escalation_user_ids` is a `UUID[]`.

## How it's tested

- `test/test_schema_service.py` (`@pytest.mark.service`) — the four owned tables accept a minimal insert; the one-in-flight partial unique index rejects a second concurrently-`running` run on the same ticket. Also seeds the five sibling-owned tables (`artifacts`, `pipeline_findings`, `repo_settings`, `repo_trigger_bindings`, `pr_comments`) via raw SQL to verify the full migrated schema end-to-end, since none of those modules' service functions exist yet to drive through their public API.
