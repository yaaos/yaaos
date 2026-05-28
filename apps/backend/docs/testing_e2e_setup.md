# testing/e2e_setup

> Programmatic test-data control surface — `/api/testing/*` routes used by e2e specs (and ad-hoc local seeding) to drive yaaos into known states.

## Purpose

Dev-only HTTP surface so each Playwright spec composes its own preconditions in `beforeEach` rather than depending on batch-seeded fixtures. Excluded from production wheel builds; every route guards on `yaaos_env == "dev"` and returns 404 otherwise — same shape FastAPI returns for an unmounted route, so prod scans can't detect the surface.

## Public interface

`service.py` exposes pure-data helpers for use without HTTP: `DEFAULT_ORG_ID`, `is_dev_env`, `reset`, `seed_bootstrap_owner`, `seed_github_install`, `seed_lesson`, `seed_user_with_session`, `stage_oauth_test_profile`, `read_and_clear_email_inbox`.

HTTP routes (prefix `/api/testing`):
- `POST /reset` — `DELETE FROM` every table in `Base.metadata` (FK-safe, `RESTART IDENTITY CASCADE`).
- `POST /seed/github_install` — seeds `github_app_installations` + Claude Code settings + `OrgCodingAgentRow`.
- `POST /seed/lesson` — `{repo_external_id, title, body}`. Returns `{status, lesson_id}`.
- `POST /seed/bootstrap_owner` — mint user + org + Owner membership.
- `POST /seed/user_with_session` — bind a raw session cookie to an existing or new user.
- `POST /seed/broken_integration` — `{org_slug, provider}`. Seeds a broken `mcp_credentials` row.

## Module architecture

### Seed paths use public APIs

`service.py` calls real service-layer functions — no `*Row` constructors, no cross-module model imports. Seeds emit the same audit rows and events as production writes. See [patterns.md § e2e seed paths use public APIs](patterns.md).

| Seed | Service calls |
|---|---|
| `seed_bootstrap_owner` | `identity_svc.create_user`, `create_email`, `create_oauth_identity`, `orgs.create_org`, `orgs.create_membership` |
| `seed_github_install` | `github.record_app_install`, `claude_code.set_api_key`, `orgs.install_coding_agent` |
| `seed_lesson` | `lessons.create` |

### `Base.metadata` completeness

`service.py` imports every other module's `models` at the top (side-effect imports, `# noqa: F401`). Without these, `core.database.truncate_all_tables` misses tables from modules whose HTTP routes haven't been touched yet in the current process. Adding a new module with tables means adding one import line here.

### `reset()`

`core.database.truncate_all_tables` in reverse-FK order via per-table `DELETE FROM` (RowExclusive locks only; won't deadlock against lingering SSE/WS connections).

### `seed_github_install`

Always inserts; does not check for existing rows. Pair with a fresh `reset()` in `beforeEach`. After this call the system passes every onboarding contributor check and is ready for webhooks.

### Consumed by e2e specs

`apps/e2e/tests/_helpers.ts` wraps routes via `resetStack`, `seedGithubInstall`, `seedLesson`. `resetStack` also calls `apps/fake-github/__test/reset` in parallel.

## Data owned

None. Reads and writes other modules' tables.

## How it's tested

`app/testing/e2e_setup/test/test_seed_service.py` covers `seed_bootstrap_owner`, `seed_github_install`, and `seed_lesson` — asserting persisted state and audit-row emission. Routes exercised continuously by every Playwright spec.
