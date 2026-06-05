# `.claude/` — agent harness

How per-app context reaches the model deterministically, and how the dev-* workflow is wired. This is harness config, not part of the shipped system (`docs/` owns that).

## Context delivery

- **Repo-wide** → root `CLAUDE.md` (loaded every session).
- **Per-app conventions (primary today)** → the dev-* skills/agents read each touched app's `docs/architecture.md` + `docs/patterns.md` explicitly: `dev-implement-phase` reads them before editing; `dev-architect` loads them "first" (it designs before touching files). Skills run via the Skill tool, which works in every environment including the desktop app — so this is the delivery path actually in force.
- **Per-app conventions (forward-looking)** → path-scoped rules in `rules/<app>.md` (`paths:` glob + `@`-imports of the app's docs; `rules/web-design.md` scoped to `apps/web/**/*.tsx` → `docs/design.md`). *Intended* to auto-load when a matching file is read. **Not relied upon yet** — auto-load has an open upstream bug ([anthropics/claude-code #16853](https://github.com/anthropics/claude-code/issues/16853)), only triggers on reads (not new-file writes), and doesn't load in the desktop app's server mode. Harmless and inert where unsupported; activates for free once the CLI fixes it. The `docs/*.md` stay the single source.

Mechanical conventions (imports, layering, `__all__`, table access, secrets) are enforced by `tach` / `bin/sync_modules` / `bin/check_table_access` / `semgrep` at CI — the docs/rules carry only the judgment conventions those checkers can't.

## Dev workflow — three tiers

- **Trivial** (typo, rename, one-liner, no behavior change) → edit directly. No artifacts, no conventions needed. (If path-scoped rules ever auto-load, they'd inject the app's conventions here for free — see Context delivery.)
- **Small-but-real** → `/dev-quick`: one command that elicits the goal, does a quick architecture pass (the scope gate), writes thin `requirements.md`/`architecture.md`/`plan.md` to `plan/ticket/<slug>/`, then delegates to `/dev-implement`.
- **Larger** → the full pipeline: `/dev-requirements` → `/dev-architect` → `/dev-plan` → `/dev-implement`.

`/dev-implement` is the shared executor; it runs each phase via the `dev-implement-phase` subagent (`agents/dev-implement-phase.md`), CI-to-green, commits, and opens the PR.

## Worktree caveat

Rules and skills live under `.claude/`. A git worktree includes the **full tree by default**, so `.claude/` is present and rules load there with no config. **If you ever adopt `worktree.sparsePaths` to narrow worktree checkouts, you MUST include `.claude` in the list** — otherwise rules/skills won't be checked out in subagent worktrees and per-app conventions silently stop loading. Do not set `sparsePaths` to only `.claude`: that would exclude the app code.

## Verifying what loaded — and environment limits

Verification requires the **interactive CLI**, not the desktop app. In the desktop app, Claude Code runs in server / stream-json mode where `settings.json` **hooks are not invoked** and `/memory` / `/context` report "isn't available in this environment" ([desktop #22138](https://github.com/desktop/desktop/issues/22138)). So neither the `InstructionsLoaded` hook nor the slash-command checks work there.

In a CLI session: read a file under an app, then run **`/context`** (or `/memory`) — it lists loaded `CLAUDE.md` / rule files. The matching `rules/<app>.md` should appear. Note the open bug ([anthropics/claude-code #16853](https://github.com/anthropics/claude-code/issues/16853)): path-scoped rules may not auto-load even in the CLI — if they don't show, that's the bug, and the skill-based explicit reads (see Context delivery) are what's carrying conventions.
