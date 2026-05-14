# M01 — Code Review Loop

Planning docs for the first milestone. Entry point for anyone (human or agent) picking up M01.

**Goal:** Three specialist review agents (architecture, security, style) automatically review every pull request opened on a configured repo, accept human feedback, and remember per-repo lessons across PRs.

**Status:** planned

## Reading order

| # | Doc | What it covers |
|---|---|---|
| 1 | [requirements.md](requirements.md) | What M01 does and doesn't do. The locked behavioral spec — every decision an autonomous implementation needs. **Start here.** |
| 2 | [architecture.md](architecture.md) | Stack, runtime topology, repo layout, cross-cutting concerns. The foundational architecture yaaof will carry forward across milestones. |
| 3 | [modularity.md](modularity.md) | Module model and import rules for backend and frontend. The constraints code must satisfy. |
| 4 | [backend.md](backend.md) | Backend module map: 8 core · 11 domain · 3 plugins. Per-module responsibilities and boundary decisions. |
| 5 | [domain-model.md](domain-model.md) | The abstract / code-level domain model: entities, value objects, aggregates, services, and ubiquitous language. **Read before data-model.md.** |
| 6 | [data-model.md](data-model.md) | All Postgres tables across all modules + relationships. The persistence side of the domain model. |
| 7 | [frontend.md](frontend.md) | Frontend tooling + module map: 7 core · 6 domain · shared bucket. Per-module responsibilities and boundary decisions. |
| 8 | [patterns.md](patterns.md) | Code style, testing discipline, and tooling conventions every module must follow. |
| 9 | [internals/](internals/) | Per-module deep dives for the 13 modules whose internal architecture is non-trivial. Read after the module maps. |

When M01 ships, the cross-cutting docs (`architecture.md`, `modularity.md`, `patterns.md`) get promoted to `docs/` (dropping the milestone scoping). Module-level docs (`docs/<module>.md`) are written alongside the code as it's built.
