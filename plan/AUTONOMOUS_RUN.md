# Autonomous execution — M03 then M04

> Top-level entry for a multi-milestone autonomous run. Every iteration of `/loop` reads this file first.

## Invocation

```
/loop 15m Continue autonomous execution per plan/AUTONOMOUS_RUN.md.
```

Drops a 15-minute recurring trigger. Each wake-up is a fresh Claude Code session that re-reads this file, picks up where the ledger says, and continues.

## User pre-flight (operational steps only the human can do)

Before kicking off the loop, confirm:

- [ ] `git status` is clean on `main` (or whatever base branch you want milestones to branch from).
- [ ] M03/M04 spec docs are committed (`plan/milestones/M03-settings/`, `plan/milestones/M04-mcp/`, and this file).
- [ ] **GitHub OAuth App**: verify-only callback URL added at `http://localhost:8080/api/account/github/verify/callback`. (Required by M03's User > Details GitHub-handle verification flow.)
- [ ] **Linear OAuth App** registered at linear.app/settings/api → OAuth applications. Scope `read`. Dev callback `http://localhost:8080/api/integrations/linear/callback`. `client_id` + `client_secret` in `.env`.
- [ ] **Notion OAuth App** registered at notion.so/my-integrations as a **Public** integration (not Internal). Capabilities: read content + read comments + read user info. Dev callback `http://localhost:8080/api/integrations/notion/callback`. `client_id` + `client_secret` in `.env`.

If any of these aren't done, the runner will hit blockers and record them in the appropriate `DECISIONS.md`. Better to handle them before starting.

## Milestone progress

The runner ticks these as each milestone completes. The top-level stop condition checks this list.

- [ ] **M03 — Settings + sidebar restructure** → [START_HERE.md](milestones/M03-settings/START_HERE.md)
- [ ] **M04 — MCP context for reviewer agents** → [START_HERE.md](milestones/M04-mcp/START_HERE.md)

## Per-iteration ritual

Every `/loop` wake-up:

1. **Re-read this file.** Note which milestones are unchecked.
2. **Pick the first unchecked milestone.** That's the active milestone.
3. **Switch to its `START_HERE.md`** and follow its ritual (each milestone has its own per-phase loop).
4. **When the active milestone's `PHASES.md` has zero `[ ]` remaining**: tick its box here in this file, commit, then continue to the next milestone in the same iteration if context allows.
5. **When this file has zero `[ ]` remaining in the Milestone progress list**: the autonomous run is done. Stop with `/loop clear`. Output a final summary to the conversation listing both milestones' `DECISIONS.md` contents.

## Branch strategy

- M03 work lives on branch `m03-settings`, branched from `main` at the start of M03.
- M04 work lives on branch `m04-mcp`, branched from `m03-settings`'s tip at the start of M04 (so M04 includes all M03 changes).
- Neither branch is pushed. The user reviews and pushes after the run.

## Decision protocol (applies to both milestones)

- Make any decision the spec doesn't resolve. Do not stop and ask.
- Rate your certainty 1–5.
- Certainty ≥ 3 → proceed silently.
- Certainty < 3 → append to the active milestone's `DECISIONS.md` using the format documented there.

## Compaction-survival contract

Auto-compaction may occur within a single iteration. After every compaction:

1. Re-read this file.
2. Re-read the active milestone's `START_HERE.md` and `PHASES.md`.
3. Resume at the first unchecked phase. Do not assume any in-memory state survived. Filesystem + git log are the truth.

Between iterations, the runner is a fresh session every time — the same recovery shape applies, just at iteration boundaries instead of compaction boundaries.

## What NOT to do

- Do not skip ahead to a later milestone before the current one's `PHASES.md` is fully ticked.
- Do not push branches.
- Do not modify `apps/backend/tach.toml` by hand — run `apps/backend/bin/sync_modules`.
- Do not commit `.env` files or secrets.
- Do not silently soften a failing test or assertion.
- Do not declare a milestone done until **all** of: every `[ ]` in its `PHASES.md` is `[x]`, the per-milestone completeness audit passed, the per-milestone full-CI phase exits 0.
- Do not stop the loop yourself for any reason other than "all milestones done." If you hit a real blocker, record it in the active milestone's `DECISIONS.md` and continue with the next phase that doesn't depend on the blocker.

## Stop condition

Run `/loop clear` and exit only when:

- Every `[ ]` in this file's "Milestone progress" list is `[x]`.
- The most recent iteration's final assistant message summarizes work + appends both `DECISIONS.md` files' contents.
