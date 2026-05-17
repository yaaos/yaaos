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
| `onboarding-stepper.spec.ts` | Empty DB → paste credentials + dispatch install webhook + save Anthropic key → dashboard flips to populated. |
| `pr-review-end-to-end.spec.ts` | PR opens → ticket created → reviewer posts → fake-github recorded the Review. |
| `pr-resync-reruns-review.spec.ts` | `pull_request.synchronize` triggers a fresh review run (also exercises force-push compare). |
| `secrets-refuse-to-review.spec.ts` | Diff with `AKIA…` → review skips with `secrets_detected` → refuse-to-review comment posted. |
| `manual-rereview-and-cancel.spec.ts` | Re-review through UI; cancel through API (`POST /api/reviewer/cancel`); assert `review_job.cancelled` audit entry. |
| `teach-yaaos-from-finding.spec.ts` | Open a posted finding → Teach modal → save → lesson visible on `/memory`. |
| `lesson-applied-next-review.spec.ts` | Pre-seed a lesson → run review → audit `prompt_sent` reports `lessons_count >= 1`. |
| `settings-cards-are-independent.spec.ts` | Save Anthropic without GitHub installed; save GitHub credentials with Anthropic set — no gating. |
| `sse-step-progress-live.spec.ts` | Open ticket detail, dispatch webhook, review card reaches `posted` without page reload — SSE-driven. |

Intentionally small. Coverage is golden-path user flows and critical regressions. Each spec is cheap (seconds of wall time) but still slower than backend integration tests — keep the set small.

## Per-spec preconditions

No batch-seeded fixture. Each spec drives its own preconditions in `beforeEach` using helpers from `apps/e2e/tests/_helpers.ts`. Typical pattern: `resetStack()` then `seedCredentialsAndInstall()`.

| Helper | What it does |
|---|---|
| `resetStack()` | `POST /api/testing/reset` (truncates every yaaos DB table) + `POST /__test/reset` on fake-github. Parallel. |
| `seedCredentialsAndInstall()` | `POST /api/testing/seed/credentials_and_install` — writes `github_settings`, `claude_code_settings`, active `github_app_installations`. |
| `seedLesson({repo_external_id, title, body})` | `POST /api/testing/seed/lesson`. |
| `dispatchWebhook({event, payload})` | For `pull_request` events: auto-seeds PR JSON into fake-github (so subsequent `fetch_pr` returns 200), then forwards to fake-github's `/__test/dispatch_webhook`. |
| `seedPRDiff({repo, number, diff, files})` | Sets a specific diff. Used by the secrets spec to inject `AKIA…`. |
| `seedCompareDiverged(before, after)` | Forces fake-github's `/compare` to return `"diverged"`. |
| `prPayload(opts)` | Builds the JSON shape yaaos's webhook parser accepts. |
| `postedComments()` | Fetches `/__test/posted_comments` for outbound-call assertions (both inline review comments and top-level PR comments). |

## yaaos-side test surface

`/api/testing/*` (gated on `YAAOS_ENV=dev`), owned by [`testing/e2e_setup`](../../backend/docs/testing_e2e_setup.md):

- `POST /reset` — truncates every table. No structural seeding (reviewer specialists are shipped markdown).
- `POST /seed/credentials_and_install` — sets "system ready" without going through the manifest flow or webhook handlers.
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
- Suite runtime: ~30s for all 10 specs on a warm stack.
