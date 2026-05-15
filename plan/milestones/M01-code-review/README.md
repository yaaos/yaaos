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
| 4 | [backend.md](backend.md) | Backend module map: 9 core · 8 domain · 3 plugins. Per-module responsibilities and boundary decisions. |
| 5 | [domain-model.md](domain-model.md) | The abstract / code-level domain model: entities, value objects, aggregates, services, and ubiquitous language. **Read before data-model.md.** |
| 6 | [data-model.md](data-model.md) | All Postgres tables across all modules + relationships. The persistence side of the domain model. |
| 7 | [frontend.md](frontend.md) | Frontend tooling + module map: 7 core · 6 domain · shared bucket. Per-module responsibilities and boundary decisions. |
| 8 | [patterns.md](patterns.md) | Code style, testing discipline, and tooling conventions every module must follow. |
| 9 | [internals/](internals/) | Per-module + cross-cutting deep dives (15 docs). Modules + the testing-infrastructure spec. Read after the module maps. |
| 10 | [../../design/M01-DELTAS.md](../../design/M01-DELTAS.md) | Locked deviations from the design prototype. Read this before the design files — it overrides specific design decisions. |
| 11 | [../../design/](../../design/) | UI design output (high-fidelity prototype + JSX source). **Visual reference only** — for spacing, color, typography, motion, and interaction shape. The deltas above govern what's actually in scope. |
| 12 | [design-brief.md](design-brief.md) | The input that produced the design pass. Useful as historical context (why the design looks the way it does); **not authoritative** — the deltas and planning docs supersede it where they disagree. |

## Precedence

When two sources disagree, the higher one in this list wins:

1. **Planning docs** (`requirements.md` → `internals/`, items 1–9 above) — the source of truth for behavior, data model, module boundaries, and patterns.
2. **`design/M01-DELTAS.md`** — locked corrections to the design prototype.
3. **`design/`** — visual reference for how screens should look and feel.
4. **`design-brief.md`** — historical input; rarely consulted directly.

If `design/README.md` says X and `M01-DELTAS.md` says not-X, do not-X. If `M01-DELTAS.md` says Y and a planning doc says not-Y, do not-Y. Never escalate from a design file to override a planning doc — surface the conflict instead.

When M01 ships, the cross-cutting docs (`architecture.md`, `modularity.md`, `patterns.md`) get promoted to `docs/` (dropping the milestone scoping). Module-level docs (`docs/<module>.md`) are written alongside the code as it's built.
