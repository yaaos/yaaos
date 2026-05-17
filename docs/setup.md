# Setup

How to get yaaos running. Covers the Docker stack (recommended) and the no-Docker local dev path.

## Prerequisites

- Docker + Docker Compose v2 (for the standard path).
- A GitHub account with permission to create GitHub Apps on the org you want reviewed.
- An Anthropic API key (yaaos shells out to the Claude Code CLI).
- For local-dev webhook testing: a smee.io channel URL (free; relays GitHub webhooks to your laptop).

## 1. Clone and configure

- Clone the repo.
- Copy `.env.sample` to `.env`.
- Generate the at-rest encryption key (32 bytes URL-safe base64) and paste it into `.env` as `YAAOS_ENCRYPTION_KEY`. Recipe: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Persist this key out-of-band (1Password, secrets manager) — losing it forces every operator to re-enter their credentials via the Settings UI.
- Leave `DATABASE_URL` at the default; the compose stack provisions Postgres with matching credentials (`yaaos:yaaos@postgres:5432/yaaos`).
- Optional: set `OTEL_EXPORTER_OTLP_ENDPOINT` if you want spans exported. Unset → OTel disabled silently.

The full env-var list is in [`apps/backend/docs/core_config.md`](../apps/backend/docs/core_config.md).

## 2. Bring up the stack

From the repo root:

- `docker compose -f docker/docker-compose.yml --env-file .env up -d --build` brings up Postgres + the yaaos backend (which serves the API on `:8080` and the bundled SPA).
- Visit `http://localhost:8080`. The dashboard renders the onboarding stepper because no GitHub App is installed and no Anthropic key is set.

## 3. Create the GitHub App (Manifest Flow)

- Decide where webhooks should land:
  - **Production:** the public URL of your yaaos deployment + `/api/github/webhook`.
  - **Laptop dev:** start a smee tunnel (`smee --url https://smee.io/<your-channel> --target http://localhost:8080/api/github/webhook`), use the smee channel URL as the webhook URL.
- On the dashboard's GitHub App card, enter the webhook URL and click **Create GitHub App**. The browser submits a manifest to `https://github.com/settings/apps/new`; you confirm on GitHub; GitHub redirects back to `/api/github/manifest-callback`, which exchanges the temporary code for App ID + slug + PEM + webhook secret and stores them encrypted.
- GitHub then redirects you straight to the install picker for the new App. Choose which repos yaaos can see and confirm. The install webhook fires back to yaaos and the GitHub card flips to "installed".

Manual escape hatch: under the GitHub card's "Already have an App? Enter it manually" toggle, paste the App ID / slug / PEM / webhook secret directly. Useful for headless setups or App reuse.

## 4. Set the Anthropic API key

- On the dashboard's Model API key card, paste your Anthropic key and Save. yaaos probes `GET /v1/models` against `api.anthropic.com` to verify the key authenticates; the badge stays red until the probe succeeds. A typo or revoked key keeps the dashboard in onboarding state.

The key is encrypted at rest with the Fernet key from your `.env`. It's never written to a workspace, never logged, and never echoed in audit payloads.

## 5. First review

- Open a PR on a repo the App can see (not a draft, not a fork).
- yaaos receives the `pull_request.opened` webhook, creates a ticket, schedules one review run, provisions a workspace, and invokes the Claude Code CLI. The parent reviewer dispatches yaaos-* subagents (architecture, security, line-level, tests, docs, conditional skill) via the Task tool, synthesizes their findings, and posts one Review back to GitHub.
- The Tickets page in the UI shows the ticket with live SSE updates as the job transitions `queued → running → posted`.

## Local dev (without Docker)

For iterating on the FE / BE without the full container build:

- **Postgres** — either run `docker compose -f docker/docker-compose.yml --env-file .env up -d postgres` (just the DB), or use a native install. Match `DATABASE_URL` in `.env`.
- **Backend** — from `apps/backend/`: `uv sync` then `uv run uvicorn app.main:app --reload --port 8080`. The reloader picks up Python changes.
- **Frontend** — from `apps/web/`: `pnpm install` then `pnpm dev`. Vite serves the SPA on `:5173` and proxies `/api/*` to `:8080`.
- **Claude Code CLI** — install on the host (`npm install -g @anthropic-ai/claude-code`) or skip by setting `YAAOS_CODING_AGENT_STUB=1`, which swaps in deterministic stub responses (no real LLM calls).

## Test stack

The e2e suite has its own self-contained stack — Postgres + `apps/fake-github` + yaaos backend with stubbed coding agent. Run `apps/e2e/bin/ci` for the one-shot up → run → down cycle. See [`apps/e2e/docs/README.md`](../apps/e2e/docs/README.md).

## Live API reference

When the backend is running, `http://localhost:8080/docs` is interactive Swagger UI and `http://localhost:8080/redoc` is the reference-style rendering. Both are generated from the live OpenAPI schema; they're the authoritative endpoint catalogue.

## Production considerations (deferred)

yaaos is currently a POC. The standard production-hardening items (auth + RBAC, rate limiting, security headers, real workspace isolation via containers or VMs, CLI agent retention pinning, audit-log pruning) are tracked in [`plan/`](../plan/). Read [`docs/system-architecture.md`](system-architecture.md) for what the security baseline actually is today.
