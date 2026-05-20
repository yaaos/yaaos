# START HERE — M04 autonomous execution

> Read this top to bottom before any work. Re-read after every context compaction and at every `/loop` iteration boundary.

## Invocation

This milestone is driven by the top-level [plan/AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md). You arrive here when M03 is complete and M04 is the first unchecked milestone.

If invoked outside that loop:

```
Execute the milestone at plan/milestones/M04-mcp/START_HERE.md. Follow it exactly.
```

## Files that govern this run

- [AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md) — top-level multi-milestone ledger and ritual.
- This file (`START_HERE.md`) — M04-specific ritual.
- [PHASES.md](PHASES.md) — M04 ledger. Checkboxes are the source of truth.
- [requirements.md](requirements.md) — locked spec.
- [architecture.md](architecture.md) — module layout, data model, refresh serialization, proxy lifecycle.
- [implementation-plan.md](implementation-plan.md) — phased build order, prose detail.
- [DECISIONS.md](DECISIONS.md) — append-only log of low-certainty decisions.

## One-time setup (only on the first iteration that touches M04)

1. Verify `git status` is clean on the current branch (should be `m03-settings` if you just completed M03).
2. If branch `m04-mcp` doesn't yet exist: `git checkout m03-settings && git checkout -b m04-mcp`. M04 branches from M03's tip — it includes all M03 changes.
3. If `m04-mcp` already exists: `git checkout m04-mcp`. Use the branch's current state as truth.
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
5. `git add` changed files. Commit: `M04 Phase <N>: <short summary>`.
6. Edit `PHASES.md`: change every `[ ]` for this phase to `[x]`. Commit: `M04 Phase <N>: tick ledger`.
7. Move to next phase. Do not stop while context budget allows.

## Decision protocol

You will hit ambiguities. Do not stop and ask.

- Make the best decision.
- Rate your certainty 1–5.
- Certainty ≥ 3: proceed silently.
- Certainty < 3: append to [DECISIONS.md](DECISIONS.md) in the format documented there. Then proceed.

## Final phases (baked into PHASES.md)

- **Phase 8 — completeness audit.** Same shape as M03's audit. Walk every section of `requirements.md`; for each requirement, prove it shipped. Verify tests (triplet + E2E), security posture (role-gating, encryption, validation), observability (org_id + user_id propagation). Fix gaps inline.
- **Phase 9 — full CI green.** All three CI scripts exit 0 on a fresh checkout; no flakes, no skips.
- **Phase 10 — handoff.** Tick M04 in `AUTONOMOUS_RUN.md`; both milestones now done; trigger `/loop clear` per the top-level ritual.

Both audit and CI phases have explicit checklist items in `PHASES.md`. Treat them like any other phase.

## Definition of "milestone done"

All of these must be true before ticking M04's box in `AUTONOMOUS_RUN.md`:

- `grep -n '\[ \]' plan/milestones/M04-mcp/PHASES.md` returns zero matches.
- Phase 8 (completeness audit) items all `[x]` with concrete proof noted in commit messages.
- Phase 9 (full CI) verifies all three CI scripts exit 0 on a fresh checkout.
- `git status` on branch `m04-mcp` is clean.
- After confirming all four: tick the M04 box in [AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md). Both milestones now done → run `/loop clear` and output a final summary listing both `DECISIONS.md` files' contents.

## Compaction-survival contract

Compaction happens. After every compaction:

1. Re-read this file.
2. Re-read `PHASES.md`.
3. Resume at the first unchecked phase. Filesystem + git log are the truth.

## What NOT to do

- Do not skip ahead to a later phase before the current phase's items are all checked.
- Do not silently soften a failing test or assertion.
- Do not modify `apps/backend/tach.toml` by hand — run `apps/backend/bin/sync_modules`.
- Do not commit `.env` files or secrets.
- Do not push the branch.
- Do not declare the milestone done until **all** Definition-of-done items are true.
- Do not stop the loop yourself except via `/loop clear` after Phase 10 (when both M03 and M04 are ticked in AUTONOMOUS_RUN.md).
