# <one-line plan summary>

Phases are CI-clean but not feature-shippable until final phase.

## Phase 1 — <goal>

- **Goal:** <one line; what's true after>
- **Vertical slice:** <boundaries crossed — front→back→storage where applicable. Mocks only where necessary.>
- **Files touched:**
  - <path>
- **Tests added:**
  - <tier (unit / service / e2e)> · <test name>
- **Doc updates:**
  - <apps/<app>/docs/*.md> · <system doc>
- **Rollback:** <undo notes, especially for migrations. Omit if nothing reversible.>

## Phase 2 — <goal>

<same shape>

## Phase N — Verify intent (final, non-code)

- **Goal:** confirm every intent.md use case "After" behavior is real and docs are current.
- **Steps:**
  - Run `apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/agent/bin/ci`, `apps/e2e/bin/ci`.
  - Re-read `intent.md`; walk each use case "After" against the running system.
  - Confirm doc updates from prior phases are landed and current.

## Open questions

- <phase-level unknowns — distinct from architecture.md's architectural ones>
