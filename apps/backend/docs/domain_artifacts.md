# domain/artifacts

> Produced-document storage; one row per artifact version.

## Purpose

Owns the `artifacts` table — the only place a pipeline stage's produced document lives. There is no separate lineage/descriptor entity: the lineage ("the ticket's requirements document") is the `(ticket_id, stage_name)` group, a composite key, not a row. Read-only for humans; revisions arrive only via instruct (same stage, same run), send-back (an earlier stage, same run), or `start_rerun_from_stage` (a new run) — all `domain/pipelines` engine re-entry paths. Written by `domain/pipelines`' skill-stage dispatch (`engine._handle_skill_stage_event`) on every `completed`-outcome terminal event.

## Public interface

`Artifact` (full VO, body included), `ArtifactGroup` / `ArtifactMeta` (metadata-only, grouped-by-stage view), the function surface (`store`, `mark_final`, `latest_final`, `list_for_ticket`, `get`), and `ArtifactNotFoundError`. HTTP routes: `GET /api/artifacts?ticket_id=`, `GET /api/artifacts/{id}` — see § HTTP endpoints below.

## Module architecture

### Entities

- **Artifact** — one produced document version. Bodies are immutable; `mark_final` is the module's only mutation (never touches `body`).

### Key value objects

- **ArtifactGroup** / **ArtifactMeta** — the version-dropdown shape: versions grouped by `stage_name`, metadata only (no bodies).

### Core user flows

- **Store a version** — `store` inserts a new non-final row; `version` is per-`(ticket_id, stage_name)` max+1 (one-run-per-ticket serializes writers, so no concurrent-insert race to guard against). Body is immutable from here on.
- **Finalize** — `mark_final` flips `is_final` — the module's only mutation, and it never touches `body`. No org check: the sole caller (the pipelines engine) addresses a row it just created in the same run, before any HTTP org-scoping context exists.
- **Read** — `latest_final` (engine input assembly + re-run read-through inheritance) only ever sees `is_final` rows, so a stage failing mid-loop leaves only non-final rows visible in `list_for_ticket`/`get` but never fed downstream. `list_for_ticket` groups by `stage_name` (the version-dropdown shape), metadata only. `get` returns the full body; raises `ArtifactNotFoundError`.

### State machines

None — an artifact version is either non-final or final (`mark_final` is a one-way flip, never reversed).

## HTTP endpoints

`domain/artifacts/web.py`, prefix `/api/artifacts`, both `ORG_SCOPED`, gated by `Action.TICKETS_READ`.

| Method | Path | Response | Errors |
|---|---|---|---|
| GET | `/api/artifacts?ticket_id={id}` | 200 `{artifacts: ArtifactGroup[]}` | 400 `no_org_context` |
| GET | `/api/artifacts/{id}` | 200 `{id, stage_name, version, iteration, is_final, body, run_id, created_at}` | 404 |

## Data owned

- `artifacts` — one row per document version. `UNIQUE(ticket_id, stage_name, version)`; `run_id` FK → `pipeline_runs`, `stage_execution_id` FK → `stage_executions` (both owned by `domain/pipelines`).

## How it's tested

- `test/test_artifacts_service.py` (`@pytest.mark.service`) — version sequencing across repeated `store` calls for the same `(ticket_id, stage_name)`; `mark_final` gates `latest_final` (a non-final row is invisible to it); HTTP reads (`list_for_ticket` grouping shape, `get` 404 on unknown id).
- `domain/pipelines/test/test_skill_stage_service.py` exercises `store`/`mark_final` end-to-end via the engine's skill-stage dispatch.
