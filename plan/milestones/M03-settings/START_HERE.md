# START HERE — M03 autonomous execution

> Read this top to bottom before any work. Re-read after every context compaction and at every `/loop` iteration boundary.

## Invocation

This milestone is driven by the top-level [plan/AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md). You arrive here when M03 is the first unchecked milestone in that file.

If you're invoked outside that loop, the one-line entry is:

```
Execute the milestone at plan/milestones/M03-settings/START_HERE.md. Follow it exactly.
```

## Files that govern this run

- [AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md) — top-level multi-milestone ledger and ritual.
- This file (`START_HERE.md`) — M03-specific ritual.
- [PHASES.md](PHASES.md) — M03 ledger. Checkboxes are the source of truth.
- [requirements.md](requirements.md) — locked spec.
- [architecture.md](architecture.md) — module layout, data model, routing.
- [implementation-plan.md](implementation-plan.md) — phased build order, prose detail.
- [DECISIONS.md](DECISIONS.md) — append-only log of low-certainty decisions.

## One-time setup (only on the first iteration that touches M03)

1. Verify `git status` is clean on the current branch. If not, stop and surface — this is the only stop-and-surface.
2. If branch `m03-settings` doesn't yet exist: `git checkout main && git pull && git checkout -b m03-settings`.
3. If it does exist: `git checkout m03-settings`. Use the branch's current state as truth.
4. Read `PHASES.md`. Find the first unchecked `[ ]`.

## The ritual (every phase)

For each phase, in order:

1. **Re-read this file, `PHASES.md`, and the relevant phase block in `implementation-plan.md`.**
2. Work the unchecked items in that phase, in listed order.
3. Follow standing rules in `CLAUDE.md`: TDD (red-green-refactor), update docs in the same commit, no hand-edits to `tach.toml`, no backward-compat shims, fix root causes not symptoms.
4. When the phase's items appear done:
   - Run `apps/backend/bin/ci` if backend changed.
   - Run `apps/web/bin/ci` if web changed.
   - Run `apps/e2e/bin/ci` if Playwright tests changed.
   - All relevant CI exits 0. Fix and re-run if not. Do not advance.
5. `git add` changed files. Commit: `M03 Phase <N>: <short summary>`.
6. Edit `PHASES.md`: change every `[ ]` for this phase to `[x]`. Commit: `M03 Phase <N>: tick ledger`.
7. Move to next phase. Do not stop while context budget allows.

## Decision protocol

You will hit ambiguities. Do not stop and ask.

- Make the best decision.
- Rate your certainty 1–5.
- Certainty ≥ 3: proceed silently.
- Certainty < 3: append to [DECISIONS.md](DECISIONS.md) in the format documented there. Then proceed.

## Final phases (baked into PHASES.md)

The last two phases of M03 are:

- **Phase 13 — completeness audit.** Re-read every section of `requirements.md`. For each requirement, grep the codebase + docs to confirm it shipped. Verify test coverage (triplet tests for every protected endpoint, E2E for every user-visible flow). Verify security posture (every new endpoint role-gated, every secret encrypted, every untrusted input validated). Verify observability (org_id + user_id propagated in logs/traces for every new code path). Fix gaps inline. Audit details + checklist in `PHASES.md`.
- **Phase 14 — full CI green.** All of `apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/e2e/bin/ci` exit 0 with no warnings, no skipped tests, no flakes. Run them on a fresh checkout of the branch to make sure side-effects in your working directory aren't masking failures. Fix everything that fails.

Both phases have explicit checklist items in `PHASES.md`. Treat them like any other phase.

## Definition of "milestone done"

All of these must be true before ticking M03's box in `AUTONOMOUS_RUN.md`:

- `grep -n '\[ \]' plan/milestones/M03-settings/PHASES.md` returns zero matches.
- Phase 13 (completeness audit) items all `[x]` with concrete proof noted in commit messages.
- Phase 14 (full CI) verifies `apps/backend/bin/ci`, `apps/web/bin/ci`, `apps/e2e/bin/ci` all exit 0.
- `git status` on branch `m03-settings` is clean.
- After confirming all four: tick the M03 box in [AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md), commit (`M03: milestone complete`), and continue to M04 in the same iteration if context allows. Otherwise exit cleanly; the next loop iteration starts M04.

## Compaction-survival contract

Compaction happens. After every compaction:

1. Re-read this file.
2. Re-read `PHASES.md`.
3. Resume at the first unchecked phase. Do not assume any in-memory state survived. Filesystem + git log are the truth.

## What NOT to do

- Do not skip ahead to a later phase before the current phase's items are all checked.
- Do not silently soften a failing test or assertion.
- Do not modify `apps/backend/tach.toml` by hand — run `apps/backend/bin/sync_modules`.
- Do not commit `.env` files or secrets.
- Do not push the branch.
- Do not write a "status checkpoint" mid-milestone in lieu of phase commits. If you can commit another phase, commit another phase.
- Do not declare the milestone done until **all** Definition-of-done items are true.
