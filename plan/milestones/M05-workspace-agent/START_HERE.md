# START HERE — M05 autonomous execution

> Read this top to bottom before any work. Re-read after every context compaction and at every `/loop` iteration boundary.

**Status:** ready for autonomous execution. Design is locked: all twelve audit topics resolved (see [requirements.md § Strategic gaps](requirements.md#strategic-gaps--all-resolved-in-this-milestone)). `PHASES.md` ledger is the source of truth for execution.

## Invocation

This milestone is driven by the top-level [plan/AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md). You arrive here when M04 is complete and M05 is the first unchecked milestone.

If invoked outside that loop:

```
Execute the milestone at plan/milestones/M05-workspace-agent/START_HERE.md. Follow it exactly.
```

## Files that govern this run

- [AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md) — top-level multi-milestone ledger and ritual.
- This file (`START_HERE.md`) — M05-specific ritual.
- [PHASES.md](PHASES.md) — M05 ledger. Checkboxes are the source of truth.
- [requirements.md](requirements.md) — locked spec, scope, decisions.
- [architecture.md](architecture.md) — module layout, data model, lifecycles, protocol, contracts.
- [implementation-plan.md](implementation-plan.md) — phased build order, prose detail.
- [DECISIONS.md](DECISIONS.md) — append-only log of low-certainty decisions.

## One-time setup (only on the first iteration that touches M05)

1. Verify `git status` is clean on the current branch (should be `m04-mcp` if you just completed M04).
2. If branch `m05-workspace-agent` doesn't yet exist: `git checkout m04-mcp && git checkout -b m05-workspace-agent`. M05 branches from M04's tip.
3. If `m05-workspace-agent` already exists: `git checkout m05-workspace-agent`. Use the branch's current state as truth.
4. Read `PHASES.md`. Find the first unchecked `[ ]`.

## The ritual (every phase)

For each phase, in order:

1. **Re-read this file, `PHASES.md`, and the relevant phase block in `implementation-plan.md`.**
2. Work the unchecked items in that phase, in listed order.
3. Follow standing rules in `CLAUDE.md`: TDD (red-green-refactor), update docs in the same commit, no hand-edits to `tach.toml`, no backward-compat shims, fix root causes not symptoms.
4. When the phase's items appear done:
   - Run `apps/backend/bin/ci` if backend changed.
   - Run `apps/web/bin/ci` if web changed.
   - Run `apps/agent/bin/ci` if agent (Go) changed.
   - Run `apps/e2e/bin/ci` if Playwright tests changed.
   - All relevant CI exits 0. Fix and re-run if not. Do not advance.
5. `git add` changed files. Commit: `M05 Phase <N>: <short summary>`.
6. Edit `PHASES.md`: change every `[ ]` for this phase to `[x]`. Commit: `M05 Phase <N>: tick ledger`.
7. Move to next phase. Do not stop while context budget allows.

## Decision protocol

You will hit ambiguities. Do not stop and ask.

- Make the best decision.
- Rate your certainty 1–5.
- Certainty ≥ 3: proceed silently.
- Certainty < 3: append to [DECISIONS.md](DECISIONS.md) in the format documented there. Then proceed.

## Final phases (baked into PHASES.md)

- **Completeness audit.** Walk every section of `requirements.md`; for each requirement, prove it shipped. Verify the contract is enforced equally by `InMemoryWorkspaceProvider` and `RemoteAgentWorkspaceProvider` (same E2E run on both). Verify trace linkage (one trace ID from webhook to comment posted). Verify cleanup failsafes (fault-injection tests).
- **Full CI green.** All relevant CI scripts exit 0 on a fresh checkout; no flakes, no skips.
- **Handoff.** Tick M05 in `AUTONOMOUS_RUN.md`; run `/loop clear` per the top-level ritual.

Both audit and CI phases have explicit checklist items in `PHASES.md`. Treat them like any other phase.

## Definition of "milestone done"

All of these must be true before ticking M05's box in `AUTONOMOUS_RUN.md`:

- `grep -n '\[ \]' plan/milestones/M05-workspace-agent/PHASES.md` returns zero matches.
- Completeness audit items all `[x]` with concrete proof noted in commit messages.
- Full-CI phase verifies all CI scripts exit 0 on a fresh checkout.
- `git status` on branch `m05-workspace-agent` is clean.
- Both `InMemoryWorkspaceProvider` and `RemoteAgentWorkspaceProvider` pass the same end-to-end PR review E2E test.
- After confirming all five: tick the M05 box in [AUTONOMOUS_RUN.md](../../AUTONOMOUS_RUN.md), run `/loop clear`, output a final summary listing `DECISIONS.md` contents.

## Compaction-survival contract

Compaction happens. After every compaction:

1. Re-read this file.
2. Re-read `PHASES.md`.
3. Resume at the first unchecked phase. Filesystem + git log are the truth.

## What NOT to do

- Do not start autonomous execution while the strategic gaps in `requirements.md` are unresolved — the milestone is still in design.
- Do not skip ahead to a later phase before the current phase's items are all checked.
- Do not silently soften a failing test or assertion.
- Do not let `InMemoryWorkspaceProvider` cut corners on invariants — the contract is the contract.
- Do not modify `apps/backend/tach.toml` by hand — run `apps/backend/bin/sync_modules`.
- Do not commit `.env` files or secrets.
- Do not push the branch.
- Do not declare the milestone done until **all** Definition-of-done items are true.
- Do not stop the loop yourself except via `/loop clear` after the handoff phase (when M05 is ticked in AUTONOMOUS_RUN.md).
