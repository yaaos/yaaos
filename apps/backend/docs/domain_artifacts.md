# domain/artifacts

> Produced-document storage; one row per artifact version.

## Purpose

Owns the `artifacts` table — the only place a pipeline stage's produced document lives. There is no separate lineage/descriptor entity: the lineage ("the ticket's requirements document") is the `(ticket_id, stage_name)` group, a composite key, not a row. Read-only for humans; revisions arrive only via instruct/re-run once the run engine (`domain/pipelines`) drives them. Does not yet own any runtime behavior — every `service.py` function raises `NotImplementedError`.

## Public interface

`Artifact` (full VO, body included), `ArtifactGroup` / `ArtifactMeta` (metadata-only, grouped-by-stage view), the stub function surface (`store`, `mark_final`, `latest_final`, `list_for_ticket`, `get`), and `ArtifactNotFoundError`. No HTTP routes yet.

## Module architecture

### Entities

- **Artifact** — one produced document version. Bodies are immutable; `mark_final` is the module's only mutation (never touches `body`).

### Key value objects

- **ArtifactGroup** / **ArtifactMeta** — the version-dropdown shape: versions grouped by `stage_name`, metadata only (no bodies).

### Core user flows

Every service function raises `NotImplementedError` — the table and signatures are the module's current substance.

### State machines

None — an artifact version is either non-final or final (`mark_final` is a one-way flip, never reversed).

## Data owned

- `artifacts` — one row per document version. `UNIQUE(ticket_id, stage_name, version)`; `run_id` FK → `pipeline_runs`, `stage_execution_id` FK → `stage_executions` (both owned by `domain/pipelines`).

## How it's tested

- `domain/pipelines/test/test_schema_service.py` seeds a minimal `artifacts` row via raw SQL (this module's service functions don't exist yet to drive it through the public API) and asserts `is_final` defaults to `false`.
