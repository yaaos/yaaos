# <one-line plan summary>

Phases are CI-clean but not feature-shippable until final phase.

Each phase is a vertical slice — one behavior end-to-end across the boundaries it touches. Horizontal phases (all migrations, then all handlers, then all UI) are refused by /dev-plan.

Each phase block is the contract with a fresh subagent. Restate load-bearing facts at the type level; cite verified `file:line` for reuse and current code. `requirements.md` / `architecture.md` are read-on-demand, not preloaded.

<!-- Anti-patterns refused at write-time:
     • NEVER paste code excerpts (current or target) in phase blocks — the cite IS the current shape; architecture.md's type-level signature IS the target shape.
     • NEVER `[question]` in Notes for implementation — forks decide here or move to Blocking handoff (which re-blocks the precondition).
     • NEVER signature-by-reference ("use the signature from architecture.md") — restate the type-level signature inline if it's load-bearing for the phase.
     • NEVER an Acceptance line that isn't falsifiable in one sentence ("works correctly" fails; "POST /api/foo returns 200 with the new schema for the seeded ticket; the audit row lands with kind=foo.created" passes). -->

## Phase 1 — <goal>

- **Goal:** <one line; what's true after>
- **Acceptance:** <one falsifiable sentence; how an outside observer confirms the goal is met. Demonstrably true against the running system, not "tests pass">
- **Size:** mechanical | many-decision | mixed — <one-line why; for `mixed`, name the seam it was/wasn't split along>
- **Context to load:**
  - `apps/<app>/docs/<layer>_<module>.md` — <one-line why>
  - `<path>:<line>` — <function / pattern to reuse, one-line why>
  - On demand: `plan/ticket/<slug>/requirements.md`, `plan/ticket/<slug>/architecture.md § <section>`
- **Vertical slice:** <user-observable behavior delivered OR integration risk retired> · <boundaries crossed, front→back→storage where applicable>
- **Changes per file:**
  - `<path>` · <what changes — symbol/block in words, NOT a code excerpt> · <why> · current code @ `<path>:<line>`
  - `<path>` · <what> · <why> · <cite or "new file">
- **Load-bearing target shapes** (restate only the type-level signatures this phase changes — pull from `architecture.md § Interface changes` / `§ Data model changes`):
  - `<funcName>`: `async def funcName(...) -> ReturnType` — raises ExceptionA
  - `<endpoint>`: `POST /api/foo` — request {field: type}, response {field: type}, errors 4xx codes
- **Tests added:**
  - <tier (unit / service / e2e)> · <test name> (setup: `<fixture / helper / seam>` @ `<path>`)
- **Doc updates:**
  - `<apps/<app>/docs/*.md>` · <one-line what's updated>
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
     out-of-scope; already-decided things → not questions. Forks the planner spotted but
     didn't resolve → they live HERE (which re-blocks the precondition), not in Notes. -->

- None.

## Notes for implementation

> Forward-looking material for dev-implement — reuse pointers, gotchas, and non-blocking *observations*. Informs but does NOT block. Self-label each bullet. Omit (or "None.") if nothing surfaced.

<!-- Forbidden here: `[question]`. A question implies a deferred fork; forks decide in the
     phase block or move to Blocking handoff. The implementer cannot reach the user, so a
     [question] left here becomes a guess at execution time. -->

- [idea] <reuse pointer or approach hint>
- [watch out] <a trap the executor should know>
