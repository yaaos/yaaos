# e2e — architecture

> Internal structure of the Playwright end-to-end test suite.

## Structure

- `tests/` — one spec file per user journey. Each spec is self-contained: it drives its own preconditions via `_helpers.ts` before asserting.
- `tests/_helpers.ts` — shared utilities (`resetStack`, `seedGithubInstall`, `dispatchWebhook`, etc.). The only shared surface inside the suite.
- `playwright.config.ts` — single-worker, no parallelism, 60 s per test, Chromium only.

## Runtime dependencies

- Postgres + backend + `fake-github` brought up via `docker/docker-compose.test.yml`.
- Backend must run with `YAAOS_ENV=dev` (enables `/api/testing/*` reset/seed routes).
- No real GitHub or Anthropic calls — `fake-github` intercepts all outbound VCS traffic; `YAAOS_CODING_AGENT_STUB=1` stubs review LLM calls.

## Scope

Covers only user-visible behavior that cannot be verified by backend service tests: OAuth redirect flow, SSE live updates in the browser, route navigation, role-gated UI rendering. Backend business logic is tested at the service tier.
