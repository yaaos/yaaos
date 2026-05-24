# testing/e2e_setup

> Programmatic test-data control surface — `/api/testing/*` routes used by e2e specs (and ad-hoc local seeding) to drive yaaos into known states.

## Purpose

A small dev-only HTTP surface so each Playwright spec composes its own preconditions in `beforeEach` rather than depending on a batch-seeded fixture. Three routes: truncate-and-reseed-structural-data, a "make yaaos ready" credentials + install seeder, and a single-row lesson inserter. Lives in the `testing/` layer (above `plugins/`) so it can depend on every domain + plugin model. Excluded from production wheel builds; every route also guards on `yaaos_env == "dev"` and returns 404 otherwise — same shape FastAPI returns for an unmounted route, so prod scans can't detect the surface exists.

## Public interface

`__init__.py` exports nothing — the module's job is the HTTP surface (side-effect import of `web.py`).

`service.py` exposes pure-data helpers backend integration tests can call without going through HTTP: `M01_ORG_ID`, `is_dev_env`, `reset`, `seed_github_install`, `seed_lesson`, `truncate_all_tables`.

HTTP routes (prefix `/api/testing`):

- `POST /reset` — truncate every table in `Base.metadata` (FK-safe, `RESTART IDENTITY CASCADE`). No structural seeding — reviewer specialists are shipped markdown files, not DB rows. Post-call: every table empty.
- `POST /seed/github_install` — `{org_login: str = "acme", target_org_slug?: str}`. Seeds an active `github_app_installations` row + Claude Code settings on the chosen org. Platform GitHub App credentials come from env vars; no per-org credential seed.
- `POST /seed/lesson` — `{repo_external_id, title, body}`. Returns `{status, lesson_id}`.

Every route calls `_guard_dev()` → raises `HTTPException(404)` outside dev.

## Module architecture

### Why the testing layer

The testing layer is the only place allowed to depend on every other module's `models` (see `docs/modularity.md`). Seed helpers deliberately ignore strict modularity boundaries — they drive tests and never run in prod.

### Populating `Base.metadata`

`service.py` imports every other module's `models` at the top (audit_log, workspace, lessons, pull_requests, reviewer, tickets, claude_code, github). Each import-for-side-effect (`# noqa: F401`) forces SQLAlchemy to register that module's tables on `Base.metadata`, which `truncate_all_tables` walks. Without these imports, a module whose HTTP routes haven't been touched in the current process might not have its tables loaded, and the truncate would miss them.

Adding a new module with tables means adding one line here. Otherwise `/reset` silently leaves the new tables non-empty between specs and tests cross-contaminate.

### `reset()`

Truncates `Base.metadata.sorted_tables` (reverse order, `RESTART IDENTITY CASCADE`). Reverse-order list is belt-and-braces for non-CASCADE engines. Empty schema short-circuits.

Reviewer specialists are shipped as markdown files in `app/domain/coding_agent/reviewers/` and installed to `~/.claude/agents/` by the claude_code plugin at backend bootstrap. No DB-level structural seeding needed. Lessons, credentials, install rows are test data and must be seeded explicitly.

### `seed_github_install(*, org_login="acme", target_org_slug=None)`

Does not check for existing rows; always inserts. Callers should pair with a fresh `reset()` in the same `beforeEach`. Writes:

- `GitHubAppInstallationRow` with `install_external_id="fake-install-1"`, status `"active"`, given `account_login`.
- `ClaudeCodeSettingsRow` with encrypted placeholder Anthropic key.

Platform GitHub App credentials live in `yaaos_github_app_*` env vars (set on the test compose). After this seed the system passes every onboarding contributor check and is ready for webhooks. The matching webhook payload (`installation: {id: "fake-install-1"}`) is built by `apps/e2e/tests/_helpers.ts`.

### `seed_lesson(*, repo_external_id, title, body)`

Inserts a single `LessonRow` with `plugin_id="github"`. Returns the generated UUID. Title chosen by caller; duplicate detection (if needed) lives in the spec.

### `is_dev_env()`

Centralizes the `yaaos_env == "dev"` check. Every route delegates here via `_guard_dev()`.

### Consumed by e2e specs

`apps/e2e/tests/_helpers.ts` wraps these routes via `resetStack`, `seedGithubInstall`, `seedLesson`. Each spec composes preconditions in `beforeEach`; no batch-seeded fixture. `resetStack` also calls fake-github's `/__test/reset` in parallel so both stacks return to a known floor in one round-trip.

## Data owned

None. Reads and writes other modules' tables.

## How it's tested

`app/testing/e2e_setup/test/` exists but holds only `__init__.py` — the routes are exercised by every Playwright spec in `apps/e2e/` via `resetStack` / `seedGithubInstall` / `seedLesson` in `beforeEach`. Stale-data dependence would surface as flake; coverage is effectively continuous.
