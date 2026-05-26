# <one-line architecture summary>

> Current state lives in [./requirements.md § Current state](./requirements.md#current-state). This doc is target + delta only.

## Approach

<short narrative of technical direction. Each load-bearing claim that's a *change* cites the current `file:line` it diverges from inline — e.g., "shift dispatcher from polling (`apps/backend/app/domain/reviewer/queue.py:200`) to event-driven".>

## Boundaries touched

- **Service boundaries:** <backend↔web, backend↔agent, etc.>
- **Module-within-service boundaries:** <module ↔ module>

## Entities & value objects

| Name | Kind | New/Changed | Lives in | Notes |
|---|---|---|---|---|
| <Entity> | entity / value object | new / changed | <service.module> | <new: one-line rationale. changed: `was @ path:line → is`.> |

Notes-column format: `new` rows write a one-line rationale; `changed` rows write `was @ path:line → is <new>`.

## Interface changes

> Coherence check: each boundary's add / change / delete set must form an internally consistent interface — no mixed styles, no granularity drift, no redundant endpoints, no disagreeing payload conventions.

### <Boundary A>

**Current anchor:** `<path:line>` — <canonical current entry-point for this boundary (handler, queue consumer, route)>

| Change | Signature / endpoint / payload / event | Notes |
|---|---|---|
| added | <sig> | <one-line rationale; which UC(s) exercise it, or "infra"> |
| changed | <sig> | `was: <sig> @ path:line → is: <new sig>` — <which UC(s) exercise it> |
| deleted | <sig> | `was: <sig> @ path:line` |

<repeat per boundary, each with its own Current anchor>

## Sequence diagrams

<ASCII, one block per use case with non-trivial sequence. Each block carries today (top) and after (bottom), separated by a horizontal rule. Cite the current entry-point `path:line` above the "today" half. Mark entities. Embed inline AND save the combined block to diagrams/<uc-slug>.txt — one file per use case, both states inside.>

<If no sequence changes: write "No sequence changes." and omit the diagrams/ directory entirely.>

## Use case walkthroughs

> For each use case in [./requirements.md § Use cases](./requirements.md#use-cases), trace the path through the architecture. Bullets, not prose. Names entities and interfaces from the tables above — does not redefine them.

### <actor> — <goal>

- **Trigger:** <what starts the flow>
- **Path:** <step 1: entity / interface called> → <step 2> → <step 3> → ...
- **Data crossing boundaries:** <payload shape names, not full schemas>
- **Diagram:** <link to diagrams/<uc-slug>.txt, or "no sequence change">

<repeat per use case — one walkthrough per use case in requirements.md>

## Data model changes

- **Tables:** <added / changed / dropped. `changed` and `dropped` cite current migration / model `path:line`; `changed` writes `was → is`.>
- **Columns:** <added / changed / dropped. Same format rule as Tables.>
- **Migrations:** <forward + rollback notes>

## Open questions

- <architectural-level unknowns — distinct from requirements.md and plan.md lists>
