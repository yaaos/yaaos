# e2e

> Playwright suite that drives the full test stack from the browser.

## Purpose

End-to-end tests for yaaos's user-visible journeys. Each spec covers one user goal top-to-bottom — webhook arrival, reviewer posting, lesson creation, settings save. Runs against `docker-compose.test.yml` (Postgres + fake-github + backend with stub coding agent), so zero real-GitHub / real-Anthropic dependency.

Why e2e despite per-module integration tests: integration tests run in-process; e2e tests run the real ASGI server, real browser, real cross-container HTTP — catching wire-format bugs, lifespan-order bugs, and frontend ↔ backend integration cracks.

## How to run

- **One-shot** (up → tests → down): `apps/e2e/bin/ci`.
- **Single spec**: `pnpm exec playwright test <name>` from `apps/e2e/`.
- **UI mode** (debug traces, time-travel): `pnpm exec playwright test --ui`.
- **Headed** (visible browser): `pnpm exec playwright test --headed`.
- **Keep stack running** across multiple invocations: bring it up with `docker compose -f docker/docker-compose.test.yml up -d --wait`, run specs, tear down with `down -v`.

Prereqs (one-time): `pnpm install` + `pnpm exec playwright install chromium` from `apps/e2e/`.

## Spec inventory

| File | User journey |
|---|---|
| `accessibility.spec.ts` | axe-core WCAG AA sweep across anchor pages. |
| `focus-reset.spec.ts` | Route change moves focus to the new page's `<h1>`/`<main>`. |
| `github-install-handshake.spec.ts` | GitHub App Manifest-Flow install handshake end-to-end. |
| `integrations-and-multi-org.spec.ts` | Integration provider settings + switching between orgs. |
| `login-and-membership.spec.ts` | Login, membership resolution, role-gated navigation. |
| `pipeline-run-overview.spec.ts` | Seeded paused run renders the attention block; approve continues it live over SSE; non-responder sees disabled actions + "Waiting on {names}.". |
| `pipeline-run-tabs.spec.ts` | Runs tab stage rows with boundary outcomes; Artifacts tab version dropdown + rendered markdown. |
| `pipeline-settings-crud.spec.ts` | Pipeline from template → boundary edit → cycle-rejection banner → referenced-delete block; builder sees no Pipelines link. |
| `repo-settings-crud.spec.ts` | Trigger binding round-trip + chip; protected-mode inversion confirm; path-set + owners round-trip; `unconfigured` badge. |
| `session-died-redirect.spec.ts` | Dead session cookie hard-redirects to `/login`. |
| `sso-flow.spec.ts` | SAML SSO login flow. |
| `workspace-agent-graceful-shutdown.spec.ts` | Agent drain → self-exit lifecycle reflected live on the fleet page. |
| `workspaces-admin-cancel-shutdown.spec.ts` | Admin cancels an in-flight agent shutdown. |
| `workspaces-admin-drain.spec.ts` | Admin drains an agent from the fleet page. |
| `workspaces-agents.spec.ts` | Fleet page sections/states for active/draining/unconfigured/inactive agents. |
| `workspaces-builder-readonly.spec.ts` | Builder sees the fleet read-only — no admin affordances. |

Intentionally small. Coverage is golden-path user flows and critical regressions. Each spec is cheap (seconds of wall time) but still slower than backend integration tests — keep the set small.

## Per-spec preconditions

No batch-seeded fixture. Each spec drives its own preconditions in `beforeEach` using helpers from `apps/e2e/tests/_helpers.ts`. Typical pattern: `resetStack()` then `seedGithubInstall()`.

| Helper | What it does |
|---|---|
| `resetStack()` | `POST /api/testing/reset` (truncates every yaaos DB table) + `POST /__test/reset` on fake-github. Parallel. |
| `seedGithubInstall()` | `POST /api/testing/seed/github_install` — writes `claude_code_settings` + an active `github_app_installations` row. Bypasses the install handshake; never pair with the install-handshake spec. Platform GitHub App credentials come from `yaaos_github_app_*` env vars. |
| `seedLesson({repo_external_id, title, body})` | `POST /api/testing/seed/lesson`. |
| `seedPausedRun(...)` | `POST /api/testing/seed/paused_run` — a pipeline run parked at an `always_hitl` boundary with an open pause row (drives the ticket Overview specs). |
| `dispatchWebhook({event, payload})` | For `pull_request` events: auto-seeds PR JSON into fake-github (so subsequent `fetch_pr` returns 200), then forwards to fake-github's `/__test/dispatch_webhook`. |
| `seedPRDiff({repo, number, diff, files})` | Sets a specific diff. Used by the secrets spec to inject `AKIA…`. |
| `seedCompareDiverged(before, after)` | Forces fake-github's `/compare` to return `"diverged"`. |
| `prPayload(opts)` | Builds the JSON shape yaaos's webhook parser accepts. |
| `postedComments()` | Fetches `/__test/posted_comments` for outbound-call assertions (both inline review comments and top-level PR comments). |

## yaaos-side test surface

`/api/testing/*` (gated on `APP_MODE=dev`), owned by [`testing/e2e_setup`](../../backend/docs/testing_e2e_setup.md):

- `POST /reset` — truncates every table. No structural seeding (reviewer specialists are shipped markdown).
- `POST /seed/github_install` — sets "system ready" without going through the install handshake (writes the install row + Claude Code settings).
- `POST /seed/lesson` — inserts a single `LessonRow`.

## fake-github contract

See [`apps/fake-github/docs/README.md`](../../fake-github/docs/README.md). Relevant for specs: `POST /__test/dispatch_webhook`, `POST /__test/seed_pr` (auto-called by `dispatchWebhook`), `POST /__test/seed_diff`, `GET /__test/posted_comments`.

## Why no batch-seed fixture

A prior shape ran `bin/seed_test_data` at container startup; every spec inherited seeded state. That made `onboarding-stepper.spec.ts` impossible to write honestly — the DB was never empty. Per-spec seeding makes each journey's preconditions explicit, and seed-shape refactors touch one module (`testing/e2e_setup`), not a Python script + docker-compose + several specs.

## Reading test artefacts

- Playwright HTML report: `apps/e2e/playwright-report/index.html`.
- Test results / traces: `apps/e2e/test-results/` — each failed test has `error-context.md` and `trace.zip`.
- Backend logs during a run: `docker compose -f docker/docker-compose.test.yml logs yaaos`.
- fake-github logs: same compose command with `logs fake-github`.

## Tech

- Playwright 1.48 + TypeScript.
- Own `package.json`; pnpm workspace member.
- `playwright.config.ts` — `workers: 1`, `fullyParallel: false`, `retries: 0`, 60s per test.
- Suite runtime: a few minutes for the full set on a warm stack.
