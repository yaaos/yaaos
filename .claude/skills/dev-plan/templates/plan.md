# <one-line plan summary>

Phases are CI-clean but not feature-shippable until final phase.

Each phase is a vertical slice — one behavior end-to-end across the boundaries it touches. Horizontal phases (all migrations, then all handlers, then all UI) are refused by /dev-plan.

Each phase block is the contract with a fresh subagent. Restate load-bearing facts; cite `file:line` for reuse. `requirements.md` / `architecture.md` are read-on-demand, not preloaded.

## Phase 1 — <goal>

- **Goal:** <one line; what's true after>
- **Size:** mechanical | many-decision | mixed — <one-line why; for `mixed`, name the seam it was/​wasn't split along>
- **Context to load:**
  - `apps/<app>/docs/<layer>_<module>.md` — <one-line why>
  - `<path>:<line>` — <function / pattern to reuse, one-line why>
  - On demand: `plan/ticket/<slug>/requirements.md`, `plan/ticket/<slug>/architecture.md`
- **Vertical slice:** <user-observable behavior delivered OR integration risk retired> · <boundaries crossed, front→back→storage where applicable>
- **Files touched:**
  - <path>
- **Tests added:**
  - <tier (unit / service / e2e)> · <test name>
- **Doc updates:**
  - <apps/<app>/docs/*.md> · <system doc>
- **Rollback:** <undo notes, especially for migrations. Omit if nothing reversible.>

## Phase 2 — <goal>

<same shape>

## Phase N — Verify requirements (final, non-code)

- **Goal:** confirm every requirements.md use case "After" behavior is real and docs are current.
- **Steps:**
  - Run `apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/agent/bin/ci`, `apps/e2e/bin/ci`.
  - Re-read `requirements.md`; walk each use case "After" against the running system.
  - Confirm doc updates from prior phases are landed and current.

## Blocking handoff questions

> Owned by this stage. Must be empty before `/dev-implement` runs.

<!-- ONLY a genuine unresolved decision that needs a human answer before/during execution.
     The dev-plan precondition guarantees requirements.md + architecture.md questions are
     already resolved, so "None." is the expected default. NOT a notes/assumptions/risks
     dump: assumptions → state them in the phase; risks → Rollback; deferred scope →
     out-of-scope; already-decided things → not questions. -->

- None.

## Notes for implementation

> Forward-looking material for dev-implement — reuse pointers, gotchas, and non-blocking questions that surfaced while planning. Informs but does NOT block. Self-label each bullet. Omit (or "None.") if nothing surfaced.

- [idea] <reuse pointer or approach hint>
- [watch out] <a trap the executor should know>
