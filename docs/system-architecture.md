# System architecture

How the apps fit together and the conventions spanning them. App-internal architecture lives in each app's own docs.

## Apps

- `apps/backend/` — FastAPI service; serves API + bundled SPA. Only app in production.
- `apps/web/` — React SPA (Vite); built into the backend image.
- `apps/fake-github/` — peer service used only by the test stack.
- `apps/e2e/` — Playwright suite driving the test stack.
- `docker/` — production + test compose.
- `plan/` — future-tense planning.

`web` is bundled into the backend image at build. `fake-github` and `e2e` only run from `docker/docker-compose.test.yml`.

## Runtime topology

- One Docker image runs FastAPI, serves the built SPA, and runs background work as in-process `asyncio` coroutines via `core/primitives.spawn()`. Periodic loops (workspace reaper, GitHub catch-up poller) start in FastAPI's `lifespan`.
- Claude Code CLI baked into the image; spawned once per review run inside the ticket's workspace. The parent reviewer dispatches `yaaos-*` subagents (architecture, security, line-level, tests, docs, conditional skill) via the Task tool and synthesizes their findings. Subagent definitions are static markdown files installed into `~/.claude/agents/` at backend bootstrap. The CLI owns all LLM communication — yaaos makes zero direct LLM calls.
- Postgres holds all state. Single DB; each module owns its tables by convention.
- OpenTelemetry collector recommended but not required; `core/observability` skips SDK setup if `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.

## Inter-app flows

### PR open → review posted

1. GitHub (or `fake-github` in tests) sends HMAC-signed `pull_request.opened` to `POST /api/github/webhook`.
2. `plugins/github` verifies HMAC, parses into a `VCSEvent`, hands to `domain/intake`.
3. `domain/intake` upserts PR (`domain/pull_requests`) + ticket (`domain/tickets`), calls `reviewer.schedule_review`.
4. `domain/reviewer` creates ONE review_job and spawns the handler coro.
5. Handler provisions the workspace and calls `coding_agent.review` once. The parent Claude Code agent dispatches `yaaos-*` subagents in parallel via the Task tool, synthesizes their findings (re-reads cited code to verify, deduplicates, ranks), and returns one merged result. Handler posts a single `vcs.Review` to GitHub with each finding tagged by its `source_agent` subagent.

Every state transition writes to `audit_log`. SSE events publish for the SPA.

### UI live update via SSE

SPA mounts one `EventSource` on `GET /api/events` at app root. Each event invalidates TanStack Query caches:

| Event `kind` | Invalidates |
|---|---|
| `ticket_status_changed` | `["tickets"]`, `["tickets", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]` |
| `review_job_status_changed` | `["reviewer", "jobs", id]`, `["tickets", id, "audit"]`, `["reviewer", "metrics"]`, `["tickets"]` |
| `review_job_step_progress` | `["reviewer", "jobs", id]` only — in-place row update |

Events carry `pr_id` + `review_job_id` (no `agent_id` — one job per review run).

Polling (5s / 3s) remains as a safety net.

### GitHub App auth chain

1. Operator creates the App via the Manifest Flow (`POST /api/github/manifest-callback` exchanges the temporary code for App ID + slug + PEM + webhook secret).
2. Credentials encrypted at rest with `cryptography.Fernet`, keyed by `YAAOS_ENCRYPTION_KEY`.
3. Per call: `plugins/github` signs a short-lived RS256 App JWT, exchanges it at `POST /app/installations/{id}/access_tokens` for an installation token (~1h TTL, in-memory).
4. Installation token used as Bearer for REST and `GIT_ASKPASS`-style for `git clone`.

### Test stack

`docker-compose.test.yml` brings up Postgres + `apps/fake-github` + backend with `GITHUB_API_BASE_URL=http://fake-github:8080` and `YAAOS_CODING_AGENT_STUB=1`. Plugins stubbed via `app/testing/`. E2E specs drive preconditions via `POST /api/testing/reset` + `seed/*`.

## Cross-app conventions

### Time
- UTC on the wire. Postgres `timestamptz`; Python `datetime.now(UTC)`; Pydantic emits `Z`-suffixed ISO 8601.
- Browser converts to local at render only — all FE timestamp display goes through `formatTime` / `formatDateTime` in `apps/web/src/shared/utils/ago.ts`.

### Webhook authenticity
- Inbound webhooks MUST carry `X-Hub-Signature-256`. `plugins/github` verifies HMAC against the secret in `github_settings` before dispatch.
- `apps/fake-github` signs outbound test webhooks with the same secret so the production verification path runs unchanged.
- Idempotency: `github_webhook_events` keyed by `X-GitHub-Delivery` UUID; duplicates skipped with `INSERT ... ON CONFLICT DO NOTHING`.

### Audit log
One append-only `audit_log` table owned by `core/audit_log` records business-meaningful state changes. Row carries `{id, org_id, created_at, entity_kind, entity_id, kind ("<entity>.<verb_past>"), payload (Pydantic-validated JSONB), actor}`. Reads never write. Progress steps go to structlog, not audit. Each domain module writes its own entries.

### Org scoping
Every domain function takes `org_id` kwarg; every query filters by it. One org today; discipline makes future RBAC a check, not a refactor.

### Secrets at rest
Plugin credentials encrypted in their plugin's settings table via `cryptography.Fernet` keyed by `YAAOS_ENCRYPTION_KEY`. Decrypt only at the call site. Never logged, echoed in errors, or placed in audit payloads.

### Dumb frontend
SPA renders data and dispatches actions. It does not compute verdicts, derive status, hold permissions, or own any rule the backend doesn't also enforce. FE validation is for UX immediacy; backend re-validates. See [`apps/web/docs/patterns.md`](../apps/web/docs/patterns.md).

## Stack at a glance

| Concern | Choice |
|---|---|
| Backend | Python 3.13, FastAPI |
| Frontend | Node 22, React + TanStack Router + TanStack Query + Tailwind |
| Data store | Postgres 16 |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic (hand-edited) |
| Background work | `asyncio.create_task` via `core/primitives.spawn` |
| Config | pydantic-settings (boot) + DB rows (runtime) |
| API | REST + SSE |
| Tests | pytest, Vitest, Playwright |
| Telemetry | OpenTelemetry SDK → collector → sink |

Boot-time env vars: see [`apps/backend/docs/core_config.md`](../apps/backend/docs/core_config.md).
